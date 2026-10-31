"""Exported-snapshot regressions for the final round-four privacy findings."""

import asyncio
import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager, wire
from fi_instrumentation.auto.wire import _privacy
from fi_instrumentation.auto.wire._capture import Recorder
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_safety import CHAT, URL, only, recording  # noqa: F401


def snapshot(span):
    return repr((span.name, dict(span.attributes), span.status.description,
                 [(e.name, dict(e.attributes)) for e in span.events],
                 [dict(link.attributes) for link in span.links], dict(span.resource.attributes),
                 (span.instrumentation_scope.name, span.instrumentation_scope.version,
                  dict(span.instrumentation_scope.attributes or {}))))


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("authentication", ["auth", "hook"])
@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
# S5 retains values of eight or more characters (supersedes R4 short-token masking).
@pytest.mark.parametrize("secret", ["DISPATCH_ONLY_CREDENTIAL_123456", "pK3x7Q2z"])
def test_httpx_dispatch_credentials_cover_auth_hooks_and_suppressed_calls(
        fresh_state, module, asynchronous, authentication, kind, secret):
    httpx = pytest.importorskip(module)
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app))
    _manager.init(tracer_provider=provider, span_exporter=fi, batch=False, instrumentations=["wire"])
    payload = {"choices": [{"message": {"content": secret}}]}
    received = []
    responses = []

    class Auth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = "Bearer " + secret
            yield request

    def hook(request):
        request.headers["Authorization"] = "Bearer " + secret

    async def async_hook(request):
        hook(request)

    def handle(request):
        received.append(request.headers["Authorization"])
        response = httpx.Response(200, json=payload)
        responses.append(response)
        return response

    async def async_handle(request):
        return handle(request)

    tracer = FITracer(provider.get_tracer("library"), TraceConfig())
    with tracer.start_as_current_span("outer", attributes={"gen_ai.span.kind": kind}) as span:
        with provider.get_tracer("native").start_as_current_span("intermediate"):
            if asynchronous:
                async def run():
                    options = {"auth": Auth()} if authentication == "auth" else {
                        "event_hooks": {"request": [async_hook]}}
                    async with httpx.AsyncClient(transport=httpx.MockTransport(async_handle), **options) as client:
                        return await client.post(URL, json=CHAT)
                response = asyncio.run(run())
            else:
                options = {"auth": Auth()} if authentication == "auth" else {"event_hooks": {"request": [hook]}}
                with httpx.Client(transport=httpx.MockTransport(handle), **options) as client:
                    response = client.post(URL, json=CHAT)
            assert response is responses[0]
            assert response.json() == payload
        span.set_attribute("output.value", "echo=" + secret)
        span.set_status(trace.Status(trace.StatusCode.ERROR, secret))
        span.add_event("echo", {"exception.message": secret})
    assert received == ["Bearer " + secret]
    assert _manager.flush(3000)
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == (2 if kind == "LLM" else 3)
        assert secret not in repr([snapshot(s) for s in spans])
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
def test_httpx_hook_failure_after_auth_mutation_keeps_exception_and_scrubs_exports(
        fresh_state, module, asynchronous, kind):
    httpx = pytest.importorskip(module)
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app))
    secret = "FAILED_HOOK_CREDENTIAL_123456"
    error = RuntimeError(secret)

    def hook(request):
        request.headers["Authorization"] = "Bearer " + secret
        raise error

    async def async_hook(request):
        hook(request)

    def execute():
        if asynchronous:
            async def run():
                async with httpx.AsyncClient(event_hooks={"request": [async_hook]}) as client:
                    await client.post(URL, json=CHAT)
            asyncio.run(run())
        else:
            with httpx.Client(event_hooks={"request": [hook]}) as client:
                client.post(URL, json=CHAT)

    # Both runs fail in the hook, before any network I/O, with the same object.
    with pytest.raises(RuntimeError) as baseline:
        execute()
    assert baseline.value is error
    _manager.init(tracer_provider=provider, span_exporter=fi, batch=False, instrumentations=["wire"])
    tracer = FITracer(provider.get_tracer("application"), TraceConfig())
    with pytest.raises(RuntimeError) as observed:
        with tracer.start_as_current_span("outer", attributes={"gen_ai.span.kind": kind}):
            execute()
    assert observed.value is baseline.value
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == (1 if kind == "LLM" else 2)
        assert secret not in repr([snapshot(s) for s in spans])
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("kind", ["CHAIN", "AGENT"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("agent_owned", [False, True])
def test_request_credentials_reach_every_active_ancestor(fresh_state, kind, batch, agent_owned):
    httpx = pytest.importorskip("httpx")
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    if not agent_owned:
        application = TracerProvider(shutdown_on_exit=False)
        application.add_span_processor(SimpleSpanProcessor(app))
        trace.set_tracer_provider(application)
    provider = _manager.init(span_exporter=fi, batch=batch, instrumentations=["wire"])
    if agent_owned:
        provider.add_span_processor(SimpleSpanProcessor(app))
    tracer = FITracer(provider.get_tracer("library"), TraceConfig())
    secret = "ANCESTOR_PROVIDER_KEY_123456"
    payload = {"choices": [{"message": {"content": secret}}]}
    with tracer.start_as_current_span("outer", attributes={"gen_ai.span.kind": kind}) as outer:
        with tracer.start_as_current_span("model", attributes={"gen_ai.span.kind": "LLM"}) as model:
            with provider.get_tracer("sdk").start_as_current_span("sdk-native") as native:
                with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
                    assert client.post(URL, json=CHAT, headers={"x-api-key": secret}).json() == payload
                native.set_attribute("output.value", secret)
            model.set_attribute("output.value", secret)
        outer.set_attribute("output.value", secret)
        outer.set_status(trace.Status(trace.StatusCode.ERROR, secret))
        outer.add_event("echo", {"exception.message": secret})
    assert _manager.flush(3000)
    for exporter in (app, fi):
        assert len(exporter.get_finished_spans()) == 3
        assert secret not in repr([snapshot(s) for s in exporter.get_finished_spans()])
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("header", ["x-goog-api-key", "ocp-apim-subscription-key", "anthropic-api-key", "x-secret-key"])
def test_all_supported_credential_header_names_are_redacted_without_a_request(fresh_state, header):
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app))
    secret = "RAW_HEADER_TOKEN_12345678"
    _manager.init(tracer_provider=provider, span_exporter=fi, batch=False, instrumentations=[],
                  metadata={header: secret})
    tracer = provider.get_tracer("application", attributes={header: secret})
    with tracer.start_as_current_span("headers") as span:
        span.set_attribute("http.request.header." + header, secret)
        span.set_attribute("input.value", json.dumps({"nested": {header: secret}}))
        span.add_event("event", {header: secret})
    for exporter in (app, fi):
        assert secret not in snapshot(only(exporter))
    _manager.shutdown()
    provider.shutdown()


_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


@pytest.mark.parametrize("provider", ["gemini", "ollama"])
def test_small_inline_media_never_reaches_exported_wire_input(recording, provider):
    exporter, tracer = recording
    if provider == "gemini":
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        body = {"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": _PNG}}]}]}
    else:
        url = "http://localhost:11434/api/chat"
        body = {"model": "llava", "messages": [{"role": "user", "content": "describe", "images": [_PNG]}]}
    original = json.dumps(body).encode()
    capture = Recorder(tracer).begin("POST", url, {}, original)
    assert capture is not None
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(b"{}")
    capture.finish()
    assert _PNG not in snapshot(only(exporter))
    assert json.dumps(body).encode() == original


@pytest.mark.parametrize("key", ["data", "bytes", "b64_json", "file_data", "audio_base64", "images"])
def test_explicit_media_fields_use_s6_threshold_unless_audio_or_magic(key):
    # S6 supersedes R4's blanket generic-field rule: short unknown base64 is
    # ordinary data unless audio/MIME context or recognized magic proves media.
    value = [_PNG, "YQ=="] if key == "images" else "YQ=="
    original = {key: value}
    cleaned = _privacy.scrub(original)
    if key == "audio_base64":
        assert "YQ==" not in repr(cleaned)
    else:
        assert "YQ==" in repr(cleaned)
    assert _PNG not in repr(cleaned)
    assert _PNG not in repr(_privacy.scrub({key: _PNG}))
    assert original == {key: value}


@pytest.mark.parametrize("secret", ["fiK7x9Q2", "redacted_key_123", "a_secret_123"])
def test_registered_credentials_sanitize_every_surface_idempotently(secret):
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1), trace.TraceState([("vendor", secret)]))
    scope = InstrumentationScope(secret, secret, attributes={"echo": secret})
    original = ReadableSpan(name=secret, context=ctx, parent=ctx, resource=Resource({"echo": secret}),
                            attributes={"output.value": secret, "array": [secret]},
                            events=[Event(secret, {"exception.message": secret}, 15)],
                            links=[trace.Link(ctx, {"echo": secret})],
                            status=trace.Status(trace.StatusCode.ERROR, secret),
                            start_time=10, end_time=20, instrumentation_scope=scope)
    clean = _privacy.export_span(original, TraceConfig(), [secret])
    again = _privacy.export_span(clean, TraceConfig(), [secret])
    assert snapshot(clean) == snapshot(again)
    assert clean.name == clean.status.description == "<redacted>"
    assert clean.attributes["output.value"] == clean.resource.attributes["echo"] == "<redacted>"
    assert clean.instrumentation_scope.attributes["echo"] == clean.events[0].attributes["exception.message"] == "<redacted>"
    assert clean.context.trace_state.get("vendor") == clean.links[0].context.trace_state.get("vendor") == "<redacted>"
    assert clean.links[0].attributes["echo"] == "<redacted>"
    assert original.name == secret and original.attributes["output.value"] == secret


@pytest.mark.parametrize("secret", ["fiK7x9Q2", "key_12345"])
def test_fi_credentials_scrub_all_snapshot_surfaces_without_changing_auth(fresh_state, secret):
    with MockServer() as collector:
        provider = _manager.init(api_key=secret, secret_key="SECOND_FI_SECRET_123456", base_url=collector.url,
                                 batch=False, instrumentations=[], metadata={"note": secret})
        with provider.get_tracer("application").start_as_current_span("echo") as span:
            span.set_attribute("output.value", "key=" + secret)
            span.set_status(trace.Status(trace.StatusCode.ERROR, secret))
            span.add_event("echo", {"exception.message": secret})
        assert _manager.flush(3000)
        spans = decode_spans(collector.collected)
        assert len(spans) == 1
        exported = spans[0]
        assert exported["attributes"]["output.value"] == "key=<redacted>"
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        request = ExportTraceServiceRequest()
        request.ParseFromString(collector.collected[0])
        actual = request.resource_spans[0].scope_spans[0].spans[0]
        assert actual.status.message == "<redacted>"
        assert actual.events[0].attributes[0].value.string_value == "<redacted>"
        assert json.loads(exported["resource"]["metadata"])["note"] == "<redacted>"
        auth = [r["headers"].get("x-api-key") for r in collector.requests if r["path"].endswith("/v1/traces")]
        assert auth == [secret]
    _manager.shutdown()
    provider.shutdown()
