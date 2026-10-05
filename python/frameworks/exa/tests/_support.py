"""Shared support for the real-SDK tests: a loopback Exa API and a tracer.

``FakeExa`` speaks just enough of api.exa.ai for exa-py 2.25.0: JSON for
``/search``, ``/contents`` and ``/answer``, and server-sent events when the
request body has ``"stream": true`` (``stream_search`` / ``stream_answer``).
A query of ``fail-401`` gets HTTP 401 on every route; a query of ``slow`` is
held open until the fake closes. Nothing here reaches the network beyond
127.0.0.1, and every key is a placeholder.
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Tuple

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

EXA_KEY = "placeholder-exa-key-must-not-be-exported"
FAIL_QUERY = "fail-401"
# The fake holds a "slow" request (and a "slow" stream after its first chunk)
# until the test ends, so a caller can cancel mid-call.
SLOW_QUERY = "slow"
# Response content the fake returns. None of it may reach a span.
RESULT_TITLE = "RESULT-TITLE-MUST-NOT-BE-EXPORTED"
RESULT_TEXT = "RESULT-TEXT-MUST-NOT-BE-EXPORTED"
ANSWER_TEXT = "ANSWER-TEXT-MUST-NOT-BE-EXPORTED"
STREAM_TEXT = "STREAM-TEXT-MUST-NOT-BE-EXPORTED"
CONTENT_MARKERS = (RESULT_TITLE, RESULT_TEXT, ANSWER_TEXT, STREAM_TEXT)

SEARCH_RESULTS = 2
ANSWER_CITATIONS = 1
# A stream carries its citations in two separate chunks (2, then 1), so a
# count taken from only the first or the last chunk is wrong.
STREAM_CITATION_CHUNKS = (2, 1)
STREAM_CITATIONS = sum(STREAM_CITATION_CHUNKS)


def _document(index: int) -> Dict[str, Any]:
    return {
        "id": "doc-{0}".format(index),
        "url": "https://example.com/{0}".format(index),
        "title": RESULT_TITLE,
        "text": RESULT_TEXT,
    }


def _stream_events() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    index = 0
    for count in STREAM_CITATION_CHUNKS:
        events.append({"choices": [{"delta": {"content": STREAM_TEXT}}]})
        events.append({"citations": [_document(index + n) for n in range(count)]})
        index += count
    events.append({"choices": [{"delta": {"content": STREAM_TEXT}}]})
    return events


# Chunks a fully consumed stream yields (exa-py drops the [DONE] sentinel).
STREAM_CHUNKS = len(_stream_events())


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # A cancelled client leaves a broken pipe behind; that is expected.
        return


class FakeExa:
    """Loopback stand-in for api.exa.ai."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self._release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                owner.calls.append(("POST", path, body))
                slow = body.get("query") == SLOW_QUERY
                if body.get("query") == FAIL_QUERY:
                    self._send(401, "application/json", {"error": "invalid api key"})
                elif body.get("stream"):
                    events = [
                        "data: {0}\n\n".format(json.dumps(event)) for event in _stream_events()
                    ]
                    events.append("data: [DONE]\n\n")
                    # A slow stream sends its first chunk, then stalls.
                    self._send(
                        200,
                        "text/event-stream",
                        "".join(events),
                        stall_after=len(events[0].encode("utf-8")) if slow else None,
                    )
                elif slow:
                    owner._release.wait(30)
                    self._send(200, "application/json", {"results": []})
                elif path == "/search":
                    results = [_document(i) for i in range(SEARCH_RESULTS)]
                    self._send(200, "application/json", {"requestId": "req-1", "results": results})
                elif path == "/contents":
                    results = [
                        dict(_document(i), url=url) for i, url in enumerate(body.get("urls", []))
                    ]
                    self._send(200, "application/json", {"results": results, "statuses": []})
                elif path == "/answer":
                    citations = [_document(i) for i in range(ANSWER_CITATIONS)]
                    self._send(
                        200, "application/json", {"answer": ANSWER_TEXT, "citations": citations}
                    )
                else:
                    self._send(404, "application/json", {"error": "not found"})

            def _send(
                self, status: int, content_type: str, payload: Any, stall_after: Any = None
            ) -> None:
                text = payload if isinstance(payload, str) else json.dumps(payload)
                data = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if stall_after is not None:
                    self.wfile.write(data[:stall_after])
                    self.wfile.flush()
                    owner._release.wait(30)
                    data = data[stall_after:]
                self.wfile.write(data)

            def log_message(self, *_: Any) -> None:
                return

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def paths(self) -> List[str]:
        return [path for _, path, _ in self.calls]

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> "FakeExa":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


@dataclass
class Traced:
    exporter: InMemorySpanExporter
    provider: TracerProvider

    def spans(self) -> List[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def one(self) -> ReadableSpan:
        spans = self.spans()
        assert len(spans) == 1, [span.name for span in spans]
        return spans[0]

    def wire(self) -> str:
        """Everything an exporter could send: attributes, events, status."""
        return "".join(span.to_json() for span in self.spans())


@contextlib.contextmanager
def instrumented(**options: Any) -> Iterator[Traced]:
    """Instrument Exa against a fresh in-memory provider; uninstrument after."""
    from traceai_exa import ExaInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-exa"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ExaInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        yield Traced(exporter, provider)
    finally:
        instrumentor.uninstrument()


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})
