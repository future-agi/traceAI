import asyncio
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from test_pipe import frame
from traceai_openwebui._provider import Providers, endpoint_url
from traceai_openwebui.cli import function_document


@pytest.fixture
def loopback():
    requests = []
    otlp = []
    delay = SimpleNamespace(seconds=0)
    chunks = [
        frame(
            {
                "choices": [
                    {"delta": {"content": "private-output"}, "finish_reason": "stop"}
                ]
            }
        ),
        frame({"usage": {"prompt_tokens": 11, "completion_tokens": 4}}),
        "data: [DONE]\n\n",
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/model":
                requests.append(json.loads(body))
                output = "".join(chunks).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                self.wfile.write(output)
            elif self.path == "/tracer/v1/traces":
                otlp.append((dict(self.headers), body))
                threading.Event().wait(delay.seconds)
                self.send_response(200)
                self.end_headers()
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        port=server.server_port,
        requests=requests,
        otlp=otlp,
        chunks=chunks,
        delay=delay,
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def test_released_host_loopback_model_and_otlp(host, loopback, monkeypatch):
    s, ns = host
    for name, value in {
        "OTEL_RESOURCE_ATTRIBUTES": "user.email=private@example.invalid",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=private-native-secret",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY": "/does-not-exist/synthetic-key",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE": "/does-not-exist/synthetic-cert",
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": "invalid",
        "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION": "invalid",
        "OTEL_BSP_MAX_QUEUE_SIZE": "invalid",
    }.items():
        monkeypatch.setenv(name, value)
    providers = Providers()
    s.pipe._providers = providers
    s.pipe.valves.endpoint = f"http://127.0.0.1:{loopback.port}"

    async def synthetic_provider(request, form_data, user):
        connection = http.client.HTTPConnection("127.0.0.1", loopback.port, timeout=5)
        await asyncio.to_thread(
            connection.request,
            "POST",
            "/model",
            json.dumps(form_data),
            {"Content-Type": "application/json"},
        )
        response = await asyncio.to_thread(connection.getresponse)

        async def stream():
            while chunk := await asyncio.to_thread(response.read, 7):
                yield chunk

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            background=BackgroundTask(connection.close),
        )

    ns["generate_openai_chat_completion"] = synthetic_provider
    response = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": True}, s.user
    )
    received = b"".join([x async for x in response.body_iterator])
    assert received == "".join(loopback.chunks).encode()
    assert s.pipe.flush()
    assert len(loopback.requests) == 1 and loopback.requests[0]["model"] == "synthetic"
    assert len(loopback.otlp) == 1
    raw_headers, payload = loopback.otlp[0]
    headers = {k.lower(): v for k, v in raw_headers.items()}
    assert headers["x-api-key"] == "synthetic-api"
    assert headers["x-secret-key"] == "synthetic-secret"
    assert "authorization" not in headers
    decoded = ExportTraceServiceRequest.FromString(payload)
    spans = decoded.resource_spans[0].scope_spans[0].spans
    assert len(spans) == 1 and spans[0].name == "openwebui.model"
    attrs = {a.key: a.value for a in spans[0].attributes}
    assert attrs["llm.token_count.total"].int_value == 15
    for marker in (
        b"private-input",
        b"private-output",
        b"private@example",
        b"synthetic-secret",
        b"private-native-secret",
    ):
        assert marker not in payload
    s.pipe.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:pass@example.com",
        "https://example.com?token=x",
        "file:///tmp/collector",
        "https://example.com/#token",
    ],
)
def test_unsafe_endpoint_rejected(url):
    with pytest.raises(ValueError):
        endpoint_url(url)


def test_endpoint_and_function_import():
    assert (
        endpoint_url("https://api.futureagi.com/")
        == "https://api.futureagi.com/tracer/v1/traces"
    )
    assert (
        endpoint_url("https://api.futureagi.com/tracer/v1/traces")
        == "https://api.futureagi.com/tracer/v1/traces"
    )
    document = function_document()
    namespace = {}
    exec(document["content"], namespace)  # noqa: S102 - test our generated package loader
    assert namespace["Pipe"]().valves.capture_content is False
    assert document["is_active"] is False
    assert "Filter" not in document["content"]


def test_reconfiguration_keeps_inflight_project(monkeypatch):
    closed = {}

    def create(endpoint, project, api, secret):
        event = threading.Event()
        closed[project] = event
        return SimpleNamespace(shutdown=event.set, force_flush=lambda timeout: True)

    monkeypatch.setattr("traceai_openwebui._provider.make_provider", create)
    providers = Providers()
    first, release_first = providers.acquire(("a", "first", "synthetic", "synthetic"))
    second, release_second = providers.acquire(
        ("a", "second", "synthetic", "synthetic")
    )
    assert first is not second and not closed["first"].is_set()
    release_first()
    release_first()  # idempotent terminal signals
    assert closed["first"].wait(5)
    providers.close()
    assert not closed["second"].is_set()
    release_second()
    assert closed["second"].wait(5)


def test_flush_deadline_with_slow_loopback_collector(loopback):
    loopback.delay.seconds = 0.25
    providers = Providers()
    provider, release = providers.acquire(
        (f"http://127.0.0.1:{loopback.port}", "synthetic", "synthetic", "synthetic")
    )
    provider.get_tracer("test").start_span("synthetic").end()
    release()
    try:
        started = time.monotonic()
        assert providers.flush(10) is False
        assert time.monotonic() - started < 0.1
        assert providers.flush(2000) is True
        assert len(loopback.otlp) == 1
    finally:
        providers.close()


def test_timed_out_flushes_do_not_spawn_unbounded_workers(monkeypatch):
    entered = threading.Event()
    resume = threading.Event()
    calls = []

    def flush(timeout):
        calls.append(timeout)
        entered.set()
        assert resume.wait(5)
        return True

    monkeypatch.setattr(
        "traceai_openwebui._provider.make_provider",
        lambda *args: SimpleNamespace(force_flush=flush, shutdown=lambda: None),
    )
    providers = Providers()
    _, release = providers.acquire(("a", "synthetic", "synthetic", "synthetic"))
    release()
    try:
        started = time.monotonic()
        assert providers.flush(10) is False
        assert time.monotonic() - started < 0.1
        assert entered.wait(1)
        for _ in range(5):
            assert providers.flush(1) is False
        assert len(calls) == 1
        resume.set()
        # A later flush must run anew, not reuse an older completed result that
        # may predate spans queued since that operation started.
        assert providers.flush(2000) is True
        assert len(calls) == 2
    finally:
        resume.set()
        providers.close()


def test_flush_failure_releases_worker_and_uses_fixed_diagnostic(monkeypatch, caplog):
    def fail(timeout):
        raise RuntimeError("private-collector-error")

    provider = SimpleNamespace(force_flush=fail, shutdown=lambda: None)
    monkeypatch.setattr(
        "traceai_openwebui._provider.make_provider", lambda *args: provider
    )
    providers = Providers()
    assert providers.flush(0) is True
    _, release = providers.acquire(("a", "synthetic", "synthetic", "synthetic"))
    release()
    try:
        assert providers.flush(1000) is False
        assert "tracing flush failed" in caplog.text
        assert "private-collector-error" not in caplog.text
        provider.force_flush = lambda timeout: True
        assert providers.flush(1000) is True
    finally:
        providers.close()


def test_export_cli_never_overwrites(tmp_path, monkeypatch):
    from traceai_openwebui.cli import main

    output = tmp_path / "function.json"
    monkeypatch.setattr(
        "sys.argv", ["traceai-openwebui", "export-function", "--output", str(output)]
    )
    main()
    assert json.loads(output.read_text())[0]["id"] == "traceai_openwebui"
    with pytest.raises(FileExistsError):
        main()
