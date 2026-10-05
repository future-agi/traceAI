"""Shared support for the real-SDK tests: a loopback Parallel API and a tracer.

``FakeParallel`` speaks just enough of api.parallel.ai for parallel-web 1.x:
``POST /v1/search``, ``POST /v1/extract`` and ``POST /v1/tasks/runs``. The
first search query (or, without queries, the first URL) picks the behaviour:

- ``fail-401``: HTTP 401 whose error message echoes the request's x-api-key
- ``fail-500``: HTTP 500
- ``fail-huge``: HTTP 400 whose ~24 KB error message echoes the x-api-key
- ``fail-echo``: HTTP 400 whose error message echoes the first search query
- ``slow``: held open until the fake closes (for cancellation)
- ``warn``: a successful response with two warnings
- ``echo-key-into-notice``: one oversized warning that echoes the x-api-key
- ``usage``: a successful response with usage items
- ``malformed``: HTTP 200 with none of the documented fields

Extract URLs containing ``missing`` come back in ``errors``. Nothing here
reaches the network beyond 127.0.0.1, and every key is a placeholder.
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

PARALLEL_KEY = "placeholder-parallel-key-must-not-be-exported"

FAIL_401 = "fail-401"
FAIL_500 = "fail-500"
# HTTP 400 whose error message echoes the x-api-key, then 8000 euro signs.
FAIL_HUGE = "fail-huge"
HUGE_ERROR_CHARS = 8000
# HTTP 400 whose error message echoes the first search query.
FAIL_ECHO = "fail-echo"
SLOW = "slow"
WARN = "warn"
# Echoes the request's x-api-key into one oversized warning message.
WARN_ECHO = "echo-key-into-notice"
USAGE = "usage"
MALFORMED = "malformed"

SEARCH_ID = "search_0123456789"
EXTRACT_ID = "extract_0123456789"
SESSION_ID = "session_0123456789"
SEARCH_RESULTS = 2
USAGE_ITEMS = (("sku_search", 1), ("sku_extract_excerpts", 3))

# Response content the fake returns. None of it may reach a span.
RESULT_TITLE = "RESULT-TITLE-MUST-NOT-BE-EXPORTED"
EXCERPT = "EXCERPT-TEXT-MUST-NOT-BE-EXPORTED"
FULL_CONTENT = "FULL-CONTENT-MUST-NOT-BE-EXPORTED"
ERROR_CONTENT = "EXTRACT-ERROR-CONTENT-MUST-NOT-BE-EXPORTED"
RESULT_URL_HOST = "result-host-must-not-be-exported.example"
CONTENT_MARKERS = (RESULT_TITLE, EXCERPT, FULL_CONTENT, ERROR_CONTENT, RESULT_URL_HOST)
# Server-written warning text: recorded unless outputs are hidden.
WARNING_MESSAGE = "warning message written by the server"
WARNING_TYPES = ("input_validation_warning", "warning")


def _result(index: int, url: Optional[str] = None) -> Dict[str, Any]:
    return {
        "url": url or "https://{0}/{1}".format(RESULT_URL_HOST, index),
        "title": RESULT_TITLE,
        "excerpts": [EXCERPT],
        "publish_date": "2026-10-01",
    }


def _trigger(body: Dict[str, Any]) -> str:
    queries = body.get("search_queries") or []
    if queries:
        return str(queries[0])
    urls = body.get("urls") or []
    return str(urls[0]) if urls else ""


def _decorate(trigger: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    if WARN_ECHO in trigger:
        message = "echo {0} {1}".format(headers.get("x-api-key", ""), "\u20ac" * 600)
        payload["warnings"] = [{"type": "warning", "message": message}]
    elif WARN in trigger:
        payload["warnings"] = [
            {"type": WARNING_TYPES[0], "message": WARNING_MESSAGE, "detail": {"field": EXCERPT}},
            {"type": WARNING_TYPES[1], "message": WARNING_MESSAGE},
        ]
    if USAGE in trigger:
        payload["usage"] = [{"name": name, "count": count} for name, count in USAGE_ITEMS]
    return payload


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A cancelled client leaves a broken pipe behind; that is expected.
        return


class FakeParallel:
    """Loopback stand-in for api.parallel.ai."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, str], Dict[str, Any]]] = []
        self.received = threading.Event()
        self._release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                headers = {key.lower(): value for key, value in self.headers.items()}
                owner.calls.append((path, headers, body))
                owner.received.set()
                trigger = _trigger(body)
                if FAIL_401 in trigger:
                    message = "invalid api key {0}".format(headers.get("x-api-key", ""))
                    self._send(401, {"error": {"message": message}})
                elif FAIL_500 in trigger:
                    self._send(500, {"error": {"message": "internal error"}})
                elif FAIL_HUGE in trigger:
                    message = "echo {0} {1}".format(
                        headers.get("x-api-key", ""), "\u20ac" * HUGE_ERROR_CHARS
                    )
                    self._send(400, {"error": {"message": message}})
                elif FAIL_ECHO in trigger:
                    self._send(400, {"error": {"message": "rejected query: " + trigger}})
                elif SLOW in trigger:
                    owner._release.wait(30)
                    self._send(503, {"error": {"message": "released"}})
                elif MALFORMED in trigger:
                    self._send(200, {"unexpected": True})
                elif path == "/v1/search":
                    results = [_result(i) for i in range(SEARCH_RESULTS)]
                    payload = {
                        "search_id": SEARCH_ID,
                        "session_id": body.get("session_id") or SESSION_ID,
                        "results": results,
                    }
                    self._send(200, _decorate(trigger, payload, headers))
                elif path == "/v1/extract":
                    results, errors = [], []
                    for index, url in enumerate(body.get("urls") or []):
                        if "missing" in url:
                            errors.append(
                                {
                                    "url": url,
                                    "error_type": "fetch_failed",
                                    "http_status_code": 404,
                                    "content": ERROR_CONTENT,
                                }
                            )
                        else:
                            results.append(dict(_result(index, url), full_content=FULL_CONTENT))
                    payload = {
                        "extract_id": EXTRACT_ID,
                        "session_id": body.get("session_id") or SESSION_ID,
                        "results": results,
                        "errors": errors,
                    }
                    self._send(200, _decorate(trigger, payload, headers))
                elif path == "/v1/tasks/runs":
                    self._send(
                        200,
                        {
                            "run_id": "run_1",
                            "status": "queued",
                            "is_active": True,
                            "processor": "base",
                            "created_at": "2026-10-05T00:00:00Z",
                            "modified_at": "2026-10-05T00:00:00Z",
                        },
                    )
                else:
                    self._send(404, {"error": {"message": "not found"}})

            def _send(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
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
        return [path for path, _, _ in self.calls]

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> "FakeParallel":
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


def new_provider() -> Tuple[InMemorySpanExporter, TracerProvider]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-parallel"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


@contextlib.contextmanager
def instrumented(**options: Any) -> Iterator[Traced]:
    """Instrument Parallel against a fresh in-memory provider; uninstrument after."""
    from traceai_parallel import ParallelInstrumentor

    exporter, provider = new_provider()
    instrumentor = ParallelInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        yield Traced(exporter, provider)
    finally:
        instrumentor.uninstrument()


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


def sync_client(fake: FakeParallel, **options: Any) -> Any:
    from parallel import Parallel

    options.setdefault("api_key", PARALLEL_KEY)
    options.setdefault("max_retries", 0)
    return Parallel(base_url=fake.origin, **options)


def async_client(fake: FakeParallel, **options: Any) -> Any:
    from parallel import AsyncParallel

    options.setdefault("api_key", PARALLEL_KEY)
    options.setdefault("max_retries", 0)
    return AsyncParallel(base_url=fake.origin, **options)
