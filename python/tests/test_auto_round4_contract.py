"""Round-four contracts; S2 replaces the retired reconfigure/restart test matrix."""

import asyncio
import importlib
import json
import threading
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from auto_mock_server import MockServer, CHAT_RESPONSE
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager, _registry, wire
from fi_instrumentation.auto.wire import _core, _privacy
from fi_instrumentation.auto.wire._capture import Recorder
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_round2_regressions import integration_double  # noqa: F401
from test_auto_safety import recording, only, CHAT, URL  # noqa: F401


@pytest.mark.parametrize("changed", [
    {"project_name": "other"}, {"sample_rate": 0}, {"sample_rate": 5},
    {"wire": False}, {"instrumentations": []}, {"disabled_instrumentations": ["wire"]},
    {"batch": True}, {"config": TraceConfig(hide_inputs=True)},
])
def test_init_once_ignores_later_configuration(fresh_state, monkeypatch, caplog, changed):
    first, second = InMemorySpanExporter(), InMemorySpanExporter()
    provider = _manager.init(span_exporter=first, batch=False, project_name="original", instrumentations=["wire"])
    route = _manager._state.overlays[provider]
    original = route._delegate
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_CONFIG_FILE", "/nonexistent/ignored/after-init")
    with caplog.at_level("INFO"):
        assert _manager.init(**{"span_exporter": second, **changed}) is provider
    assert caplog.text.count("already initialized") == 1
    assert route._delegate is original and route.enabled and wire._recorder.enabled
    assert not _manager._state.trace_config.hide_inputs
    with provider.get_tracer("app").start_as_current_span("one"):
        pass
    assert only(first).resource.attributes["project_name"] == "original"
    assert not second.get_finished_spans()
    _manager.shutdown()


def test_init_once_is_serialized_and_shutdown_cannot_restart(fresh_state):
    exporter = InMemorySpanExporter()
    providers = []
    threads = [threading.Thread(target=lambda: providers.append(_manager.init(
        span_exporter=exporter, batch=False, instrumentations=[]))) for _ in range(6)]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(3)
        assert not worker.is_alive()
    assert len(providers) == 6 and all(p is providers[0] for p in providers)
    assert len(_manager._state.overlays) == 1
    _manager.shutdown()
    assert _manager.init(span_exporter=InMemorySpanExporter()) is providers[0]
    assert _manager._state.stopped and wire._recorder is None


def test_auto_instrument_adds_without_rewiring_pending_or_active(fresh_state):
    first, second = TracerProvider(shutdown_on_exit=False), TracerProvider(shutdown_on_exit=False)
    _manager.auto_instrument(tracer_provider=first, instrumentations=["fake"], wire=False)
    result = _manager.auto_instrument(tracer_provider=second, instrumentations=["wire"], wire=True,
                                      config=TraceConfig(hide_inputs=True))
    assert result["fake"] == _manager.PENDING
    assert _manager._state.provider is first and not _manager._state.trace_config.hide_inputs
    import fakeailib  # noqa: F401
    import traceai_fakeailib
    assert len(traceai_fakeailib.CALLS) == 1
    assert traceai_fakeailib.CALLS[0]["tracer_provider"] is first
    assert wire._recorder.enabled
    _manager.shutdown()
    first.shutdown()
    second.shutdown()


@pytest.mark.parametrize("rate", [0, 1])
@pytest.mark.parametrize("app_sampler", [ALWAYS_OFF, ALWAYS_ON])
def test_explicit_provider_installs_supplied_exporter_and_sampling(fresh_state, rate, app_sampler):
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(sampler=app_sampler, shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app))
    assert _manager.init(tracer_provider=provider, span_exporter=fi, batch=False,
                         sample_rate=rate, instrumentations=["wire"]) is provider
    capture = wire._recorder.begin("POST", URL, {}, json.dumps(CHAT).encode())
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(json.dumps(CHAT_RESPONSE).encode())
    capture.finish()
    assert len(fi.get_finished_spans()) == rate
    assert len(app.get_finished_spans()) == (1 if app_sampler is ALWAYS_ON else 0)
    assert provider.sampler is app_sampler
    _manager.shutdown()
    provider.shutdown()


def test_export_snapshot_covers_every_surface_and_leaves_original_intact():
    secret = "EXPORTED_CREDENTIAL_123"
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1), trace.TraceState([("vendor", secret)]))
    scope = InstrumentationScope(secret, secret, "https://example.test/?sig=" + secret,
                                 {"llm.input_messages.0.message.content": "PRIVATE_INPUT"})
    original = ReadableSpan(name=secret, context=ctx, parent=ctx,
        resource=Resource({"metadata": '{"email":"person@example.com"}', "signed": "https://x/?sig=" + secret}),
        attributes={"llm.input_messages.0.message.content": "PRIVATE_INPUT", "llm.output_messages": "PRIVATE_OUTPUT",
                    "llm.invocation_parameters": '{"secret":"PRIVATE_PARAMETER"}',
                    "array": [secret, "data:audio/wav;base64," + "A" * 400],
                    "numbers": [float("nan"), float("inf"), -float("inf")]},
        events=[Event(secret, {"exception.message": secret, "input.custom": "PRIVATE_INPUT"}, 20)],
        links=[trace.Link(ctx, {"output.custom": "PRIVATE_OUTPUT", "echo": secret})],
        status=trace.Status(trace.StatusCode.ERROR, secret), start_time=10, end_time=30,
        instrumentation_scope=scope)
    clean = _privacy.export_span(original, TraceConfig(hide_inputs=True, hide_outputs=True,
                                hide_llm_invocation_parameters=True, pii_redaction=True), [secret])
    assert original.name == secret and original.attributes["llm.output_messages"] == "PRIVATE_OUTPUT"
    assert clean.start_time == 10 and clean.end_time == 30
    assert list(clean.attributes["numbers"]) == ["NaN", "Infinity", "-Infinity"]
    serialized = repr((clean.name, dict(clean.attributes), clean.status.description,
                       [(e.name, dict(e.attributes)) for e in clean.events],
                       [dict(l.attributes) for l in clean.links], dict(clean.resource.attributes),
                       clean.instrumentation_scope.name, clean.instrumentation_scope.version,
                       dict(clean.instrumentation_scope.attributes), clean.context.trace_state.items(),
                       clean.parent.trace_state.items(), clean.links[0].context.trace_state.items()))
    for forbidden in [secret, "PRIVATE_", "person@example.com", "A" * 400]:
        assert forbidden not in serialized
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    assert encode_spans([clean]).SerializeToString()


def test_sanitizer_is_bounded_and_never_calls_unknown_value_hooks():
    class Poison:
        def __str__(self):
            pytest.fail("user __str__ invoked")

        def __getattr__(self, name):
            pytest.fail("user getter invoked")

        def __call__(self):
            pytest.fail("user callable invoked")

    cycle = []
    cycle.append(cycle)
    branching = ["leaf"]
    for _ in range(32):
        branching = [branching] * 8
    assert _privacy.scrub(Poison()) == "Poison"
    assert "PRIVATE_KEY" not in _privacy.scrub('{"api_key":"PRIVATE_KEY",')
    deep = '[' * 2000 + '{"password":"PRIVATE_PASSWORD"}' + ']' * 2000
    assert "PRIVATE_PASSWORD" not in _privacy.scrub(deep)
    assert len(repr(_privacy.scrub(branching))) < 100000
    assert len(repr(_privacy.scrub(cycle))) < 2000
    assert _privacy.mask_attributes({"custom": Poison()}, TraceConfig(), ())["custom"] == "Poison"


def test_sanitizer_failure_keeps_identity_and_timing_only(monkeypatch):
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1))
    original = ReadableSpan("PRIVATE", ctx, attributes={"input.value": "PRIVATE"},
                            start_time=10, end_time=20, resource=Resource({"secret": "PRIVATE"}))
    monkeypatch.setattr(_privacy, "mask_attributes", lambda *a: (_ for _ in ()).throw(RuntimeError("mask failed")))
    clean = _privacy.export_span(original, TraceConfig())
    assert clean.context.trace_id == 123 and clean.context.span_id == 456
    assert clean.start_time == 10 and clean.end_time == 20
    assert clean.name == "span" and not clean.attributes and not clean.resource.attributes


@pytest.mark.parametrize("kind", ["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL", "CHAIN", "AGENT", "TOOL"])
def test_model_context_marker_survives_intermediate_native_span(recording, kind):
    exporter, tracer = recording
    recorder = Recorder(tracer)
    native = tracer.__wrapped__
    with tracer.start_as_current_span("library", attributes={"gen_ai.span.kind": kind}):
        with native.start_as_current_span("sdk-native"):
            capture = recorder.begin("POST", URL, {}, json.dumps(CHAT).encode())
            assert (capture is None) == (kind in {"LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"})
    assert recorder.begin("POST", URL, {}, json.dumps(CHAT).encode()) is not None


@pytest.mark.parametrize("name", ["agno", "beeai"])
def test_adopts_real_singleton_without_resetting_native_handle(fresh_state, monkeypatch, name):
    frameworks = Path(__file__).resolve().parents[1] / "frameworks"
    monkeypatch.syspath_prepend(str(frameworks / name))
    module = importlib.import_module("traceai_" + name)
    cls = getattr(module, "AgnoInstrumentorWrapper" if name == "agno" else "BeeAIInstrumentorWrapper")
    instance = cls()
    instance._is_instrumented = True
    field = "_agno_instrumentor" if name == "agno" else "_beeai_instrumentor"
    handle = object()
    setattr(instance, field, handle)
    try:
        assert _registry.SETUPS[name](module, object(), TraceConfig()) == "already instrumented"
        assert getattr(instance, field) is handle and instance.is_instrumented
    finally:
        instance._is_instrumented = False
        cls._instance = None


def test_failed_setup_detaches_partial_processor(fresh_state, monkeypatch):
    provider = TracerProvider(shutdown_on_exit=False)
    partial = SpanProcessor()
    def failed(module, supplied, config):
        supplied.add_span_processor(partial)
        raise RuntimeError("failed after adding")
    item = _registry.Integration("failed_setup", ("fakeailib",), "fake", "failed_setup", "failed", kind=_registry.SETUP)
    monkeypatch.setitem(__import__("sys").modules, item.module, SimpleNamespace())
    monkeypatch.setitem(_registry.SETUPS, item.entry, failed)
    monkeypatch.setattr(_registry, "ALL", (item,))
    assert _manager.auto_instrument(tracer_provider=provider, wire=False, lazy=False)[item.name] == _manager.FAILED
    assert partial not in provider._active_span_processor._span_processors
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("name", ["livekit", "pipecat"])
@pytest.mark.parametrize("batch", [False, True])
def test_real_framework_mapper_uses_actual_route(fresh_state, monkeypatch, name, batch):
    frameworks = Path(__file__).resolve().parents[1] / "frameworks"
    monkeypatch.syspath_prepend(str(frameworks / name))
    module = importlib.import_module("traceai_" + name)
    if name == "livekit":
        monkeypatch.setattr(module, "flush_on_job_shutdown", lambda *a: True)
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=batch, instrumentations=[],
                             config=TraceConfig(hide_inputs=True))
    _registry.SETUPS[name](module, provider, TraceConfig())
    attrs = {"lk.pii.user_input" if name == "livekit" else "input": "PRIVATE_INPUT"}
    with provider.get_tracer("native").start_as_current_span("native", attributes=attrs):
        pass
    assert _manager.flush(1000)
    assert only(exporter).attributes["input.value"] == "__REDACTED__"
    assert "PRIVATE_INPUT" not in repr(dict(only(exporter).attributes))
    _manager.shutdown()


@pytest.mark.parametrize("headers", [MappingProxyType({"X-API-Key": "explicit-key", "X-Secret-Key": "explicit-secret"}),
                                     __import__("requests").structures.CaseInsensitiveDict({"X-API-Key": "explicit-key", "X-Secret-Key": "explicit-secret"})])
def test_http_exporter_accepts_mapping_headers(headers):
    from fi_instrumentation.otel import HTTPSpanExporter
    exporter = HTTPSpanExporter(headers=headers)
    headers = getattr(exporter, "_headers", None) or exporter._client._headers
    assert headers["x-api-key"] == "explicit-key"
    exporter.shutdown()


def test_aiohttp_callback_abort_matches_no_agent_baseline(recording):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, tracer = recording
    error = RuntimeError("application callback failure")
    bodies = []
    async def callback(response):
        bodies.append(await response.read())
        raise error
    async def run(url):
        async with aiohttp.ClientSession(raise_for_status=callback) as session:
            with pytest.raises(RuntimeError) as caught:
                await session.post(url + "/v1/chat/completions", json=CHAT)
            assert caught.value is error
    with MockServer() as server:
        asyncio.run(run(server.url))
        wire.enable(tracer, transports=["aiohttp"])
        asyncio.run(run(server.url))
    assert bodies[0] == bodies[1]
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.OK
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["output.value"] == "pong"
    assert "error.type" not in span.attributes


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_httpx_response_identity_helpers_and_unread_stream_match_baseline(recording, module):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording
    class Response(httpx.Response):
        def helper(self):
            return "subclass-helper"
    class Body(httpx.SyncByteStream):
        def __init__(self):
            self.reads = 0
        def __iter__(self):
            self.reads += 1
            yield json.dumps(CHAT_RESPONSE).encode()
    for instrumented in [False, True]:
        body = Body()
        response = Response(200, headers={"content-type": "application/json"}, stream=body)
        if instrumented:
            wire.enable(tracer, transports=[module])
        with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as client:
            request = client.build_request("POST", URL, json=CHAT)
            returned = client.send(request, stream=True)
            assert returned is response and returned.helper() == "subclass-helper"
            assert body.reads == 0 and not returned.is_stream_consumed
            returned.close()
    assert only(exporter).attributes["traceai.wire.incomplete"]


def test_transport_timestamps_exclude_validation_and_parsing(recording, monkeypatch):
    from fi_instrumentation.auto.wire import _capture
    exporter, tracer = recording
    clock = [100]
    monkeypatch.setattr(_capture.time, "time_ns", lambda: clock[0])
    original_check, original_build = _core.check_request, _core.build
    def check(*args):
        clock[0] = 200
        return original_check(*args)
    def build(*args):
        clock[0] = 900
        return original_build(*args)
    monkeypatch.setattr(_core, "check_request", check)
    monkeypatch.setattr(_core, "build", build)
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(CHAT).encode())
    capture.on_headers(200, {"content-type": "application/json"})
    clock[0] = 300
    capture.on_chunk(json.dumps(CHAT_RESPONSE).encode())
    clock[0] = 500  # consumer/EOF/close delay is not transport time either
    capture.finish()
    span = only(exporter)
    assert span.start_time == 100 and span.end_time == 300


def test_requests_redirect_failure_keeps_final_headers_and_partial_output(recording):
    import requests
    exporter, tracer = recording
    failure = requests.ReadTimeout("transport read failure")

    class Raw:
        def stream(self, *args, **kwargs):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise failure

        def close(self):
            pass

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.request, response.url = request, request.url
            if "/redirected/" not in request.url:
                response.status_code = 307
                response.headers["Location"] = "/redirected/v1/chat/completions"
                response._content, response._content_consumed = b"", True
            else:
                response.status_code = 200
                response.headers["Content-Type"] = "text/event-stream"
                response.raw = Raw()
            return response

        def close(self):
            pass

    for instrumented in [False, True]:
        if instrumented:
            wire.enable(tracer, transports=["requests"])
        with requests.Session() as session:
            session.mount("https://", Adapter())
            with pytest.raises(requests.ReadTimeout) as caught:
                session.post(URL, json=CHAT)
            assert caught.value is failure
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["output.value"] == "partial"


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_async_httpx_identity_and_cancellation_match_baseline(recording, module):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording

    async def run(instrumented):
        entered = asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                entered.set()
                await asyncio.Event().wait()
        class Response(httpx.Response):
            def helper(self):
                return 42
        response = Response(200, headers={"content-type": "text/event-stream"}, stream=Body())
        async def handle(request):
            return response
        if instrumented:
            wire.enable(tracer, transports=[module])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            request = client.build_request("POST", URL, json=CHAT)
            result = await client.send(request, stream=True)
            assert result is response and result.helper() == 42
            task = asyncio.create_task(result.aread())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await result.aclose()
    asyncio.run(run(False))
    asyncio.run(run(True))
    assert only(exporter).attributes["traceai.wire.incomplete"]


def test_exporter_boundary_also_covers_processors_added_later(fresh_state):
    from fi_instrumentation.auto._manager import _PrivacyExporter
    provider = _manager.init(span_exporter=InMemorySpanExporter(), batch=False, instrumentations=[])
    later = InMemorySpanExporter()
    processor = SimpleSpanProcessor(later)
    provider.add_span_processor(processor)
    assert isinstance(processor.span_exporter, _PrivacyExporter)
    with provider.get_tracer("application").start_as_current_span("app") as span:
        span.set_attribute("payload", '{"api_key":"PRIVATE_API_KEY"}')
    assert "PRIVATE_API_KEY" not in only(later).attributes["payload"]
    _manager.shutdown()
    provider.shutdown()


def test_reentrant_init_from_instrumentor_returns_existing_provider(fresh_state, monkeypatch):
    original = _manager._enable
    nested = []
    def enable(integration, provider, config):
        nested.append(_manager.init(project_name="ignored"))
        return original(integration, provider, config)
    monkeypatch.setattr(_manager, "_enable", enable)
    provider = _manager.init(span_exporter=InMemorySpanExporter(), batch=False,
                             instrumentations=["fake"], lazy=False)
    assert nested == [provider]
    _manager.shutdown()


def test_nonfinite_arrays_survive_every_otlp_surface():
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    attrs = {"numbers": [1.0, float("nan"), float("inf"), -float("inf")]}
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1))
    original = ReadableSpan("numeric", ctx, resource=Resource(attrs), attributes=attrs,
        instrumentation_scope=InstrumentationScope("scope", attributes=attrs),
        events=[Event("event", attrs, 15)], links=[trace.Link(ctx, attrs)], start_time=10, end_time=20)
    clean = _privacy.export_span(original, TraceConfig())
    surfaces = [clean.attributes, clean.resource.attributes, clean.instrumentation_scope.attributes,
                clean.events[0].attributes, clean.links[0].attributes]
    for surface in surfaces:
        assert list(surface["numbers"]) == [1.0, "NaN", "Infinity", "-Infinity"]
    assert encode_spans([clean]).SerializeToString()


def test_sanitizer_uses_one_text_budget_across_surfaces(monkeypatch):
    monkeypatch.setattr(_privacy, "_MAX_TEXT", 128)
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1))
    span = ReadableSpan("bounded", ctx, resource=Resource({}),
                        attributes={"array": ["a" * 100] * 50},
                        events=[Event("event", {"payload": "b" * 100}, 15)], start_time=10, end_time=20)
    clean = _privacy.export_span(span, TraceConfig())
    assert repr(dict(clean.attributes)).count("a" * 100) == 1
    assert "b" * 100 not in repr(dict(clean.events[0].attributes))


@pytest.mark.parametrize("agent_sampled", [False, True])
def test_global_native_ancestry_suppresses_wire_and_protects_model_echo(fresh_state, agent_sampled):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=["wire"])
    from fi_instrumentation.auto._sampling import agent_provider
    native = (agent_provider(provider) if agent_sampled else provider).get_tracer("global-native")
    secret = "MODEL_REQUEST_CREDENTIAL_123"
    with native.start_as_current_span("model") as model:
        model.set_attribute("gen_ai.span.kind", "LLM")
        with native.start_as_current_span("sdk-native"):
            assert wire._recorder.begin("POST", URL, {"Authorization": "Bearer " + secret},
                                        json.dumps(CHAT).encode()) is None
        model.set_attribute("output.value", secret)
    assert len(exporter.get_finished_spans()) == 2
    assert secret not in repr([dict(s.attributes) for s in exporter.get_finished_spans()])
    _manager.shutdown()


def test_shutdown_during_install_cleans_once_and_never_publishes_active(integration_double, monkeypatch):
    item, instrumentor = integration_double
    instrumentor.entered, instrumentor.release = threading.Event(), threading.Event()
    cleanups = []
    original = instrumentor.uninstrument
    def uninstrument():
        cleanups.append(True)
        original()
    monkeypatch.setattr(instrumentor, "uninstrument", uninstrument)
    provider = TracerProvider(shutdown_on_exit=False)
    installer = threading.Thread(target=lambda: _manager.auto_instrument(
        tracer_provider=provider, instrumentations=[item.name], wire=False, lazy=False))
    installer.start()
    assert instrumentor.entered.wait(2)
    _manager.shutdown(100)
    instrumentor.release.set()
    installer.join(3)
    assert not installer.is_alive() and not instrumentor.active
    assert cleanups == [True]
    assert _manager._state.statuses[item.name].status == _manager.DISABLED
    assert not _manager._state.cleanups
    provider.shutdown()
