"""Loopback stand-in for api.replicate.com and an in-memory tracer.

``FakeReplicate`` speaks the part of the Replicate HTTP API that the official
``replicate`` 1.x client uses for predictions: create (version, official
model and deployment routes), get, cancel, the SSE stream URL, model versions
and trainings. ``RecordingTransport`` is passed to ``replicate.Client`` so the
real SDK sends every request through it: it records each request, refuses any
host other than 127.0.0.1 (no live Replicate call, no file download), and can
open a child span per request to stand in for an HTTP client instrumentation.

Every token, prompt and output is a placeholder marker.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

API_TOKEN = "r8_placeholder-token-MUST-NOT-BE-EXPORTED"

TEXT_MODEL = "acme/text-model"  # output: a list of text tokens
IMAGE_MODEL = "acme/image-model"  # output: one https file URL
FILES_MODEL = "acme/files-model"  # output: a list of https file URLs
FAIL_MODEL = "acme/fail-model"  # ends "failed" with MODEL_ERROR
SLOW_MODEL = "acme/slow-model"  # stays "processing" until canceled
ECHO_MODEL = "acme/echo-model"  # output: input["prompt"] as one string
DATA_MODEL = "acme/data-model"  # output: an inline base64 data: URI
STREAM_MODEL = "acme/stream-model"  # urls.stream: text events, then done
STREAM_FILE_MODEL = "acme/stream-file-model"  # urls.stream: file URL events
STREAM_ERROR_MODEL = "acme/stream-error-model"  # urls.stream: text, then error
STREAM_SLOW_MODEL = "acme/stream-slow-model"  # urls.stream: one event, then stalls
DEPLOYMENT = "acme/text-deployment"  # runs TEXT_MODEL

TEXT_VERSION = "5c7d5dc6dd8bf75c1acaa8565735e7986bc5b66206b55cca93cb72c9bf15ccaa"
ITERATOR_VERSION = "iter0000000000000000000000000000000000000000000000000000000000ab"

PROMPT = "PROMPT-CONTENT-MARKER"
TEXT_TOKENS = ["OUTPUT-", "TEXT-", "MARKER"]
TEXT_OUTPUT = "".join(TEXT_TOKENS)
FILE_URL = "https://files.example.invalid/OUTPUT-FILE-MARKER.png"
FILE_URLS = [FILE_URL, "https://files.example.invalid/OUTPUT-FILE-MARKER-2.png"]
MODEL_ERROR = "MODEL-ERROR-MARKER: CUDA out of memory"
STREAM_CHUNKS = ["STREAM-", "TEXT-", "MARKER"]
STREAM_TEXT = "".join(STREAM_CHUNKS)
STREAM_ERROR = "STREAM-ERROR-MARKER"
DATA_PAYLOAD = base64.b64encode(b"DATA-URI-BYTES-MARKER" * 8).decode("ascii")
DATA_URI = "data:image/png;base64," + DATA_PAYLOAD
PREDICT_TIME = 1.25
CONTENT_MARKERS = (PROMPT, TEXT_OUTPUT, "OUTPUT-FILE-MARKER", MODEL_ERROR, STREAM_TEXT)

TERMINAL = ("succeeded", "failed", "canceled")

_VERSION_MODELS = {TEXT_VERSION: TEXT_MODEL, ITERATOR_VERSION: TEXT_MODEL}


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A closed stream leaves a broken pipe behind; that is expected.
        return


@dataclass
class _Prediction:
    id: str
    model: str
    version: str
    input: Dict[str, Any]
    status: str = "starting"
    polls: int = 0
    stream: bool = False


class FakeReplicate:
    """A loopback Replicate API. ``origin`` is the base URL to give the client."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, str], Any]] = []
        self.predictions: Dict[str, _Prediction] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw) if raw else None

            def _record(self, body: Any) -> str:
                path = self.path.split("?")[0]
                headers = {key.lower(): value for key, value in self.headers.items()}
                with owner._lock:
                    owner.calls.append((self.command, path, headers, body))
                return path

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = self._record(None)
                parts = path.strip("/").split("/")
                if parts[:2] == ["v1", "predictions"] and len(parts) == 3:
                    self._json(*owner._poll(parts[2]))
                elif parts[0] == "stream" and len(parts) == 2:
                    self._stream(owner.predictions[parts[1]])
                elif parts[:2] == ["v1", "models"] and len(parts) == 6 and parts[4] == "versions":
                    self._json(200, _version_json(parts[5]))
                elif parts[:2] == ["v1", "deployments"] and len(parts) == 4:
                    self._json(200, {"owner": parts[2], "name": parts[3], "current_release": None})
                else:
                    self._json(404, {"detail": "not found", "status": 404})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                body = self._body()
                path = self._record(body)
                parts = path.strip("/").split("/")
                prefer_wait = "wait" in self.headers.get("Prefer", "")
                if parts == ["v1", "predictions"]:
                    model = _VERSION_MODELS.get(body.get("version"), TEXT_MODEL)
                    self._json(201, owner._create(model, body, prefer_wait))
                elif parts[:2] == ["v1", "models"] and parts[-1] == "predictions":
                    self._json(201, owner._create("/".join(parts[2:4]), body, prefer_wait))
                elif parts[:2] == ["v1", "deployments"] and parts[-1] == "predictions":
                    self._json(201, owner._create(TEXT_MODEL, body, prefer_wait))
                elif parts[:2] == ["v1", "predictions"] and parts[-1] == "cancel":
                    self._json(*owner._cancel("/".join(parts[2:-1])))
                elif parts[-1] == "trainings":
                    self._json(201, _training_json(parts))
                else:
                    self._json(404, {"detail": "not found", "status": 404})

            def _json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, prediction: _Prediction) -> None:
                events = owner._stream_events(prediction)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                for index, event in enumerate(events):
                    if event is None:  # stall marker
                        owner._release.wait(30)
                        return
                    self.wfile.write(event.encode("utf-8"))
                    self.wfile.flush()
                self.close_connection = True

            def log_message(self, *_: Any) -> None:
                return

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    # -- prediction lifecycle ---------------------------------------------------

    def _create(self, model: str, body: Dict[str, Any], prefer_wait: bool) -> Dict[str, Any]:
        with self._lock:
            prediction_id = "pred{0:04d}".format(next(self._ids))
            version = body.get("version") or "v-" + model.replace("/", "-")
            prediction = _Prediction(
                id=prediction_id,
                model=model,
                version=version,
                input=body.get("input") or {},
                stream=bool(body.get("stream")) or model.startswith("acme/stream"),
            )
            self.predictions[prediction_id] = prediction
            if prefer_wait and model != SLOW_MODEL and not prediction.stream:
                prediction.status = "failed" if model == FAIL_MODEL else "succeeded"
            return self._json_for(prediction)

    def _poll(self, prediction_id: str) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            prediction = self.predictions.get(prediction_id)
            if prediction is None:
                return 404, {"detail": "prediction not found", "status": 404}
            prediction.polls += 1
            if prediction.status not in TERMINAL and prediction.model != SLOW_MODEL:
                if prediction.polls == 1:
                    prediction.status = "processing"
                else:
                    prediction.status = "failed" if prediction.model == FAIL_MODEL else "succeeded"
            return 200, self._json_for(prediction)

    def _cancel(self, prediction_id: str) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            prediction = self.predictions.get(prediction_id)
            if prediction is None:
                return 404, {"detail": "prediction not found", "status": 404}
            if prediction.status not in TERMINAL:
                prediction.status = "canceled"
            return 200, self._json_for(prediction)

    def _output(self, prediction: _Prediction) -> Any:
        if prediction.status not in ("processing", "succeeded"):
            return None
        done = prediction.status == "succeeded"
        if prediction.model == IMAGE_MODEL:
            return FILE_URL if done else None
        if prediction.model == FILES_MODEL:
            return list(FILE_URLS) if done else None
        if prediction.model == ECHO_MODEL:
            return str(prediction.input.get("prompt")) if done else None
        if prediction.model == DATA_MODEL:
            return DATA_URI if done else None
        if prediction.model == FAIL_MODEL:
            return None
        return list(TEXT_TOKENS) if done else TEXT_TOKENS[:1]

    def _json_for(self, prediction: _Prediction) -> Dict[str, Any]:
        failed = prediction.status == "failed"
        terminal = prediction.status in TERMINAL
        urls = {
            "get": "{0}/v1/predictions/{1}".format(self.origin, prediction.id),
            "cancel": "{0}/v1/predictions/{1}/cancel".format(self.origin, prediction.id),
        }
        if prediction.stream:
            urls["stream"] = "{0}/stream/{1}".format(self.origin, prediction.id)
        return {
            "id": prediction.id,
            "model": prediction.model,
            "version": prediction.version,
            "status": prediction.status,
            "input": prediction.input,
            "output": self._output(prediction),
            "logs": "",
            "error": MODEL_ERROR if failed else None,
            "metrics": {"predict_time": PREDICT_TIME} if prediction.status == "succeeded" else {},
            "created_at": "2026-10-05T00:00:00.000Z",
            "started_at": None,
            "completed_at": "2026-10-05T00:00:02.000Z" if terminal else None,
            "urls": urls,
        }

    def _stream_events(self, prediction: _Prediction) -> List[Optional[str]]:
        def event(kind: str, number: int, data: str) -> str:
            lines = "".join("data: {0}\n".format(line) for line in data.split("\n"))
            return "event: {0}\nid: {1}\n{2}\n".format(kind, number, lines)

        outputs: List[Optional[str]]
        if prediction.model == STREAM_FILE_MODEL:
            outputs = [event("output", i, url) for i, url in enumerate(FILE_URLS)]
            return outputs + [event("done", 9, "{}")]
        outputs = [event("output", i, chunk) for i, chunk in enumerate(STREAM_CHUNKS)]
        if prediction.model == STREAM_ERROR_MODEL:
            return outputs[:1] + [event("error", 8, STREAM_ERROR)]
        if prediction.model == STREAM_SLOW_MODEL:
            return outputs[:1] + [None]
        return outputs + [event("done", 9, "{}")]

    # -- inspection ---------------------------------------------------------------

    def paths(self, method: Optional[str] = None) -> List[str]:
        with self._lock:
            return [path for verb, path, _, _ in self.calls if method in (None, verb)]

    def request_headers(self, method: str, path: str) -> Dict[str, str]:
        with self._lock:
            for verb, seen, headers, _ in self.calls:
                if verb == method and seen == path:
                    return headers
        raise AssertionError("no {0} {1}".format(method, path))

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> "FakeReplicate":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _version_json(version_id: str) -> Dict[str, Any]:
    if version_id == ITERATOR_VERSION:
        output = {"type": "array", "items": {"type": "string"}, "x-cog-array-type": "iterator"}
    else:
        output = {"type": "array", "items": {"type": "string"}}
    return {
        "id": version_id,
        "created_at": "2026-10-01T00:00:00.000Z",
        "cog_version": "0.9.0",
        "openapi_schema": {"components": {"schemas": {"Output": output}}},
    }


def _training_json(parts: List[str]) -> Dict[str, Any]:
    return {
        "id": "train0001",
        "model": "/".join(parts[2:4]),
        "version": parts[5] if len(parts) > 5 else "v",
        "destination": None,
        "status": "starting",
        "input": {},
        "output": None,
        "logs": "",
        "error": None,
        "created_at": None,
        "started_at": None,
        "completed_at": None,
        "urls": {},
    }


class RecordingTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """Sends only to 127.0.0.1 and records every request the SDK makes.

    With ``tracer`` set, each request runs inside a child span named
    ``HTTP <METHOD>`` so a test can check which span was current.
    """

    def __init__(self, tracer: Any = None) -> None:
        self.requests: List[Tuple[str, str]] = []
        self._tracer = tracer
        self._sync = httpx.HTTPTransport()
        self._async = httpx.AsyncHTTPTransport()

    def _check(self, request: httpx.Request) -> None:
        self.requests.append((request.method, str(request.url)))
        if request.url.host != "127.0.0.1":
            raise httpx.ConnectError("test transport refuses " + request.url.host, request=request)

    def _span(self, request: httpx.Request) -> Any:
        if self._tracer is None:
            return contextlib.nullcontext()
        return self._tracer.start_as_current_span("HTTP " + request.method)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._check(request)
        with self._span(request):
            return self._sync.handle_request(request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._check(request)
        with self._span(request):
            return await self._async.handle_async_request(request)

    def close(self) -> None:
        self._sync.close()

    async def aclose(self) -> None:
        await self._async.aclose()


def make_client(fake: FakeReplicate, transport: Optional[RecordingTransport] = None, **options: Any):
    """A real ``replicate.Client`` pointed at the fake, polling every 10 ms."""
    import replicate

    options.setdefault("api_token", API_TOKEN)
    client = replicate.Client(
        base_url=fake.origin, transport=transport or RecordingTransport(), **options
    )
    client.poll_interval = 0.01
    return client


@dataclass
class Traced:
    exporter: InMemorySpanExporter
    provider: TracerProvider
    instrumentor: Any

    def spans(self) -> List[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def replicate_spans(self) -> List[ReadableSpan]:
        return [span for span in self.spans() if span.name.startswith("replicate.")]

    def one(self) -> ReadableSpan:
        spans = self.replicate_spans()
        assert len(spans) == 1, [span.name for span in spans]
        return spans[0]

    def tracer(self) -> Any:
        return self.provider.get_tracer("test-http")

    def wire(self) -> str:
        """Everything an exporter could send: names, attributes, events, status."""
        return "".join(span.to_json() for span in self.spans())


@contextlib.contextmanager
def instrumented(**options: Any) -> Iterator[Traced]:
    """Instrument replicate against a fresh in-memory provider; uninstrument after."""
    from traceai_replicate import ReplicateInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-replicate"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        yield Traced(exporter, provider, instrumentor)
    finally:
        instrumentor.uninstrument()


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


def exception_events(span: ReadableSpan) -> List[Any]:
    return [event for event in span.events if event.name == "exception"]
