"""Shared support for the real-SDK tests: a loopback Voyage API and a tracer.

``FakeVoyage`` speaks enough of the Voyage HTTP API for voyageai 0.3.7 to
0.5.x: ``POST /v1/embeddings`` (base64 or list vectors, float and packed
binary dtypes), ``POST /v1/rerank`` and ``POST /v1/multimodalembeddings``.
The real ``voyageai.Client`` / ``AsyncClient`` reach it through
``base_url=fake.base_url``. Nothing here leaves 127.0.0.1 and every key is a
placeholder.

Models with special behaviour:

* ``fail-401``: HTTP 401 with a ``detail`` message.
* ``echo-key``: HTTP 400 whose ``detail`` echoes the bearer key back, so a
  test can prove the key is redacted from error text.
* ``rate-limit-once``: the first request gets HTTP 429, later ones succeed.
* ``slow``: the request is held until the fake closes (timeouts, cancels).
"""

from __future__ import annotations

import base64
import contextlib
import json
import struct
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

VOYAGE_KEY = "pa-placeholder-voyage-key-must-not-be-exported"
EMBED_MODEL = "voyage-3.5"
RERANK_MODEL = "rerank-2.5"

# Content the tests send or the fake returns. None of it may reach a span
# unless content capture is switched on.
TEXTS = ["TEXT-ALPHA-MUST-NOT-BE-EXPORTED", "TEXT-BETA-MUST-NOT-BE-EXPORTED"]
QUERY = "QUERY-MUST-NOT-BE-EXPORTED"
DOCUMENTS = [
    "DOC-ONE-MUST-NOT-BE-EXPORTED",
    "DOC-TWO-MUST-NOT-BE-EXPORTED",
    "DOC-THREE-MUST-NOT-BE-EXPORTED",
]
CONTENT_MARKERS = tuple(TEXTS) + (QUERY,) + tuple(DOCUMENTS)

# Vectors: every component is VECTOR_BASE + k * 2**-9, exact in float32, so
# the decoded values are predictable and searchable on the wire.
DIMENSION = 16
VECTOR_BASE = 0.3359375
VECTOR_MARKER = "0.3359375"
# Relevance score per document index. The fake returns them sorted by score
# (1, 0, 2), so a span that re-sorted or lost the order is visible.
SCORES = [0.4140625, 0.91796875, 0.16015625]
SCORE_MARKERS = ("0.4140625", "0.91796875", "0.16015625")

TOKENS_PER_TEXT = 7


def rerank_tokens(documents: int) -> int:
    return 5 + 6 * documents


def _vector(dimension: int) -> List[float]:
    return [VECTOR_BASE + k * 2**-9 for k in range(dimension)]


def _encode(values: List[Any], fmt: str) -> str:
    return base64.b64encode(struct.pack("<{0}{1}".format(len(values), fmt), *values)).decode()


def _embedding(body: Dict[str, Any]) -> Any:
    dimension = int(body.get("output_dimension") or DIMENSION)
    dtype = body.get("output_dtype") or "float"
    as_base64 = body.get("encoding_format") == "base64"
    if dtype in ("binary", "ubinary"):
        # Packed bits: eight dimensions per byte.
        values = [(-1) ** k * (k + 1) for k in range(dimension // 8)]
        fmt = "b" if dtype == "binary" else "B"
        values = [abs(v) for v in values] if dtype == "ubinary" else values
        return _encode(values, fmt) if as_base64 else values
    if dtype in ("int8", "uint8"):
        values = [k % 100 for k in range(dimension)]
        return _encode(values, "b" if dtype == "int8" else "B") if as_base64 else values
    values = _vector(dimension)
    return _encode(values, "f") if as_base64 else values


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A timed-out or cancelled client leaves a broken pipe behind.
        return


class FakeVoyage:
    """Loopback stand-in for the Voyage API (api.voyageai.com / ai.mongodb.com)."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any], bool]] = []
        self.received = threading.Event()
        self._release = threading.Event()
        self._rate_limited = False
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                authorization = self.headers.get("Authorization", "")
                with owner._lock:
                    owner.calls.append((path, body, authorization == "Bearer " + VOYAGE_KEY))
                owner.received.set()
                model = body.get("model")
                if model == "slow":
                    owner._release.wait(30)
                    self._send(503, {"detail": "released"})
                elif model == "fail-401":
                    self._send(401, {"detail": "Provided API key is invalid."})
                elif model == "echo-key":
                    key = authorization[len("Bearer ") :]
                    self._send(400, {"detail": "bad request for key {0}".format(key)})
                elif model == "rate-limit-once" and owner._take_rate_limit():
                    self._send(429, {"detail": "rate limited"})
                elif path == "/v1/embeddings":
                    texts = body.get("input")
                    texts = [texts] if isinstance(texts, str) else list(texts or [])
                    data = [
                        {"object": "embedding", "embedding": _embedding(body), "index": i}
                        for i in range(len(texts))
                    ]
                    self._send(
                        200,
                        {
                            "object": "list",
                            "data": data,
                            "model": model,
                            "usage": {"total_tokens": TOKENS_PER_TEXT * len(texts)},
                        },
                    )
                elif path == "/v1/rerank":
                    documents = list(body.get("documents") or [])
                    ranked = sorted(
                        range(len(documents)),
                        key=lambda i: SCORES[i % len(SCORES)],
                        reverse=True,
                    )
                    top_k = body.get("top_k")
                    if top_k is not None:
                        ranked = ranked[: int(top_k)]
                    data = [
                        {"index": i, "relevance_score": SCORES[i % len(SCORES)]} for i in ranked
                    ]
                    self._send(
                        200,
                        {
                            "object": "list",
                            "data": data,
                            "model": model,
                            "usage": {"total_tokens": rerank_tokens(len(documents))},
                        },
                    )
                elif path == "/v1/multimodalembeddings":
                    inputs = list(body.get("inputs") or [])
                    data = [
                        {"object": "embedding", "embedding": _vector(DIMENSION), "index": i}
                        for i in range(len(inputs))
                    ]
                    usage = {
                        "text_tokens": 3,
                        "image_pixels": 0,
                        "video_pixels": 0,
                        "total_tokens": 3,
                    }
                    self._send(200, {"object": "list", "data": data, "usage": usage})
                else:
                    self._send(404, {"detail": "not found"})

            def _send(self, status: int, payload: Dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    return

            def log_message(self, *_: Any) -> None:
                return

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self.base_url = self.origin + "/v1"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def _take_rate_limit(self) -> bool:
        with self._lock:
            if self._rate_limited:
                return False
            self._rate_limited = True
            return True

    def paths(self) -> List[str]:
        with self._lock:
            return [path for path, _, _ in self.calls]

    def bodies(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [body for _, body, _ in self.calls]

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> "FakeVoyage":
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
    """Instrument Voyage against a fresh in-memory provider; uninstrument after."""
    from traceai_voyage import VoyageInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-voyage"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = VoyageInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        yield Traced(exporter, provider)
    finally:
        instrumentor.uninstrument()


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


def event_names(span: ReadableSpan) -> List[str]:
    return [event.name for event in span.events]


def client(fake: FakeVoyage, **options: Any) -> Any:
    import voyageai

    return voyageai.Client(api_key=VOYAGE_KEY, base_url=fake.base_url, **options)


def async_client(fake: FakeVoyage, **options: Any) -> Any:
    import voyageai

    return voyageai.AsyncClient(api_key=VOYAGE_KEY, base_url=fake.base_url, **options)


def status_code(span: ReadableSpan) -> Optional[str]:
    return span.status.status_code.name
