"""Round-four verifier regressions: echoed credentials, suppression and final redirects."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation import FITracer, TraceConfig, suppress_tracing
from fi_instrumentation.auto import _manager, wire
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_safety import recording, only, CHAT, URL  # noqa: F401


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("agent_owned", [False, True])
def test_unsuppressed_native_parent_scrubs_request_credential_echo(fresh_state, batch, agent_owned):
    httpx = pytest.importorskip("httpx")
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    if not agent_owned:
        application = TracerProvider(shutdown_on_exit=False)
        application.add_span_processor(SimpleSpanProcessor(app))
        trace.set_tracer_provider(application)
    provider = _manager.init(span_exporter=fi, batch=batch, instrumentations=["wire"])
    if agent_owned:
        provider.add_span_processor(SimpleSpanProcessor(app))
    secret = "PROVIDER_REQUEST_KEY_123456"
    payload = {"choices": [{"message": {"role": "assistant", "content": secret}}]}
    native = provider.get_tracer("sdk-native")
    with native.start_as_current_span("native-sdk") as span:
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
            response = client.post(URL, json=CHAT, headers={"x-api-key": secret})
            assert response.json() == payload
        span.set_attribute("gen_ai.output.messages", json.dumps(payload))
        span.set_status(trace.Status(trace.StatusCode.ERROR, secret))
        span.add_event("echo", {"exception.message": secret})
    assert _manager.flush(3000)
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert secret not in repr([(dict(s.attributes), s.status.description,
                                    [dict(e.attributes) for e in s.events]) for s in spans])
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("agent_owned", [False, True])
def test_effective_exporter_header_credentials_scrub_content_and_resource(fresh_state, agent_owned):
    secret = "OVERRIDDEN_FI_KEY_123456"
    if not agent_owned:
        trace.set_tracer_provider(TracerProvider(shutdown_on_exit=False))
    with MockServer() as collector:
        provider = _manager.init(api_key="ORIGINAL_FI_KEY_123456", secret_key="ORIGINAL_SECRET_123456",
                                 base_url=collector.url, batch=False, instrumentations=[],
                                 headers={"X-API-Key": secret, "X-Secret-Key": "OVERRIDDEN_FI_SECRET_123456"}, metadata={"note": secret})
        with provider.get_tracer("application").start_as_current_span("echo") as span:
            span.set_attribute("output.value", secret)
        assert _manager.flush(3000)
        exported = decode_spans(collector.collected)
        assert len(exported) == 1
        assert secret not in repr(exported)
        auth = [r["headers"].get("x-api-key") for r in collector.requests if r["path"].endswith("/v1/traces")]
        assert auth == [secret]  # Sanitization must not change authentication itself.
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("kind", ["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"])
def test_suppressed_model_span_never_marks_shared_invalid_span_or_loses_later_calls(fresh_state, kind):
    import requests

    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=["wire"])
    tracer = FITracer(provider.get_tracer("library"), config=TraceConfig())
    with MockServer() as server, requests.Session() as client:
        assert client.post(server.url + "/v1/chat/completions", json=CHAT).status_code == 200
        with suppress_tracing():
            with tracer.start_as_current_span("suppressed-model", attributes={"gen_ai.span.kind": kind}):
                pass
        assert not getattr(trace.INVALID_SPAN, "_traceai_model_covered", False)
        assert not hasattr(trace.INVALID_SPAN, "_traceai_model_owner")
        assert client.post(server.url + "/v1/chat/completions", json=CHAT).status_code == 200
    assert len(exporter.get_finished_spans()) == 2
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("redirect", ["default", "explicit-off", "follow", "no-location"])
def test_httpx_final_redirect_matches_baseline_and_exports_once(recording, module, asynchronous, streamed, redirect):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording
    kwargs = {"follow_redirects": False} if redirect == "explicit-off" else {}
    options = {"follow_redirects": redirect in ("explicit-off", "follow", "no-location")}
    results = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b"redirect"

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"redirect"

    def response(request):
        if "/final/" in request.url.path:
            return httpx.Response(200, json={"choices": [{"message": {"content": "final"}}]})
        headers = {"location": "/final/v1/chat/completions"} if redirect != "no-location" else {}
        return httpx.Response(307, headers=headers, stream=AsyncBody() if asynchronous else Body())

    for enabled in (False, True):
        if enabled:
            wire.enable(tracer, transports=[module])
        if asynchronous:
            async def run():
                async def handle(request):
                    return response(request)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle), **options) as client:
                    request = client.build_request("POST", URL, json=CHAT)
                    result = await client.send(request, stream=streamed, **kwargs)
                    body = await result.aread()
                    await result.aclose()
                    return result.status_code, body, result.is_closed, [r.status_code for r in result.history]
            results.append(asyncio.run(run()))
        else:
            with httpx.Client(transport=httpx.MockTransport(response), **options) as client:
                request = client.build_request("POST", URL, json=CHAT)
                result = client.send(request, stream=streamed, **kwargs)
                body = result.read()
                result.close()
                results.append((result.status_code, body, result.is_closed, [r.status_code for r in result.history]))
    assert results[0] == results[1]
    span = only(exporter)
    assert span.attributes["http.response.status_code"] == (200 if redirect == "follow" else 307)
    if redirect == "follow":
        assert span.attributes["output.value"] == "final"


@pytest.mark.parametrize("redirect", ["off", "follow", "no-location", "not-redirect-status"])
def test_aiohttp_final_redirect_matches_baseline_and_exports_once(recording, redirect):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, tracer = recording

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            final = self.path.startswith("/final/")
            body = b'{"choices":[{"message":{"content":"final"}}]}' if final else b"redirect"
            self.send_response(200 if final else 304 if redirect == "not-redirect-status" else 307)
            if not final and redirect != "no-location":
                self.send_header("Location", "/final/v1/chat/completions")
            if redirect == "not-redirect-status":
                body = b""  # HTTP 304 has no body.
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    async def run(url):
        async with aiohttp.ClientSession() as client:
            async with client.post(url, json=CHAT, allow_redirects=redirect != "off") as response:
                return response.status, await response.read(), [r.status for r in response.history]

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        baseline = asyncio.run(run(url))
        wire.enable(tracer, transports=["aiohttp"])
        assert asyncio.run(run(url)) == baseline
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
    span = only(exporter)
    assert span.attributes["http.response.status_code"] == baseline[0]
    if redirect == "follow":
        assert span.attributes["output.value"] == "final"
