"""Deterministic regressions for provider ownership, independent routes and privacy."""

import asyncio
import importlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON

from auto_mock_server import MockServer, CHAT_RESPONSE
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager, _registry, wire
from fi_instrumentation.auto._config import AutoConfig
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_safety import recording, only  # noqa: F401
from test_auto_round3_regressions import finish, CHAT, URL


class Exporter(InMemorySpanExporter):
    def __init__(self):
        super().__init__()
        self.closed = False

    def shutdown(self):
        self.closed = True
        super().shutdown()


@pytest.mark.parametrize("flag", ["hide_input_text", "hide_inputs", "hide_input_messages"])
def test_native_input_aliases_and_events_hidden(fresh_state, flag):
    attrs = {
        "gen_ai.input.messages": '[{"role":"user","content":"PRIVATE_INPUT"}]',
        "gen_ai.system_instructions": "PRIVATE_SYSTEM",
        "gen_ai.tool.call.arguments": '{"arg":"PRIVATE_ARGUMENT"}',
        "gen_ai.input.messages.0.message.contents": '[{"text":"PRIVATE_PART"}]',
        "gen_ai.system_instructions.0.text": "PRIVATE_SYSTEM_PART",
        "gen_ai.tool.call.arguments.city": "PRIVATE_ARGUMENT_PART",
        "gen_ai.tool.parameters": '{"description":"PRIVATE_SCHEMA"}',
        "gen_ai.prompts": '["PRIVATE_PROMPT"]',
        "llm.prompt_template.variables": '{"value":"PRIVATE_VARIABLE"}',
    }
    _assert_aliases_hidden(attrs, TraceConfig(**{flag: True}))


@pytest.mark.parametrize("flag", ["hide_output_text", "hide_outputs", "hide_output_messages"])
def test_native_output_aliases_and_events_hidden(fresh_state, flag):
    attrs = {
        "gen_ai.output.messages": '[{"role":"assistant","content":"PRIVATE_OUTPUT"}]',
        "gen_ai.tool.call.result": '{"result":"PRIVATE_RESULT"}',
        "gen_ai.output.messages.0.message.contents": '[{"text":"PRIVATE_PART"}]',
        "gen_ai.tool.call.result.city": "PRIVATE_RESULT_PART",
    }
    _assert_aliases_hidden(attrs, TraceConfig(**{flag: True}))


def _assert_aliases_hidden(attrs, config):
    provider = TracerProvider(shutdown_on_exit=False)
    app, fi = Exporter(), Exporter()
    provider.add_span_processor(SimpleSpanProcessor(app))
    trace.set_tracer_provider(provider)
    _manager.init(span_exporter=fi, batch=False, instrumentations=[], config=config)
    with FITracer(provider.get_tracer("library"), config=config).start_as_current_span("library") as span:
        span.__wrapped__.set_attributes(attrs)
        span.__wrapped__.add_event("native", attrs)
    for destination in (fi, app):
        exported = only(destination)
        assert "PRIVATE_" not in str(dict(exported.attributes))
        assert "PRIVATE_" not in str([dict(e.attributes) for e in exported.events])
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("name", ["semantic_kernel", "strands"])
def test_adopts_real_native_instrumentor_without_resetting_it(fresh_state, monkeypatch, name):
    frameworks = Path(__file__).resolve().parents[1] / "frameworks"
    monkeypatch.syspath_prepend(str(frameworks / name.replace("_", "-")))
    module = importlib.import_module("traceai_" + name)
    cls = getattr(module, "SemanticKernelInstrumentor" if name == "semantic_kernel" else "StrandsInstrumentor")
    if name == "semantic_kernel":
        monkeypatch.setattr(module, "_import_modules", lambda *args: [])
    else:
        monkeypatch.setattr(cls, "_configure_strands_telemetry", lambda *args, **kwargs: None)
    instance = cls()
    provider = TracerProvider(shutdown_on_exit=False)
    instance.instrument(tracer_provider=provider)
    assert instance.is_instrumented
    if name == "strands":
        assert instance._tracer_provider is provider
    integration = _registry.Integration(name, ("fakeailib",), "real", module.__name__, name, kind=_registry.SETUP)
    monkeypatch.setattr(_registry, "ALL", (integration,))
    try:
        _manager.auto_instrument(tracer_provider=provider, wire=False, lazy=False)
        _manager.auto_instrument(instrumentations=[], wire=False)
        _manager.shutdown()
        assert instance.is_instrumented
        if name == "strands":
            assert instance._tracer_provider is provider
    finally:
        instance.uninstrument()
        provider.shutdown()


def test_replacing_owned_provider_preserves_later_app_processors(fresh_state):
    first = _manager.init(span_exporter=Exporter(), batch=False, instrumentations=[])
    app = Exporter()
    first.add_span_processor(SimpleSpanProcessor(app))
    cached = first.get_tracer("application")
    with cached.start_as_current_span("before"):
        pass
    second = TracerProvider(shutdown_on_exit=False)
    _manager.init(tracer_provider=second, instrumentations=[])
    assert not app.closed
    with cached.start_as_current_span("after"):
        pass
    assert [s.name for s in app.get_finished_spans()] == ["before", "after"]
    _manager.shutdown()
    assert not app.closed
    second.shutdown()
    first.shutdown()


@pytest.mark.parametrize("direct_write", [False, True, "stale_tuple"])
def test_concurrent_app_processor_never_becomes_setup_owned(fresh_state, monkeypatch, direct_write):
    provider = TracerProvider(shutdown_on_exit=False)
    app = Exporter()
    app_processor = SimpleSpanProcessor(app)
    ours = SpanProcessor()
    entered, release = threading.Event(), threading.Event()
    failures = []

    def setup(module, supplied, config):
        if direct_write == "stale_tuple":
            chain = supplied._active_span_processor
            snapshot = chain._span_processors
            entered.set()
            assert release.wait(5)
            chain._span_processors = (ours,) + snapshot
        elif direct_write:
            chain = supplied._active_span_processor
            with chain._lock:
                chain._span_processors = (ours,) + chain._span_processors
        else:
            # Deliberately use the real provider, as legacy/global adapters do.
            provider.add_span_processor(ours)
        entered.set()
        assert release.wait(5)
        return "installed"

    integration = _registry.Integration("sync_setup", ("fakeailib",), "fake", "traceai_sync_setup", "sync_setup", kind=_registry.SETUP)
    monkeypatch.setitem(sys.modules, integration.module, SimpleNamespace())
    monkeypatch.setattr(_registry, "ALL", (integration,))
    monkeypatch.setitem(_registry.SETUPS, integration.entry, setup)

    def install():
        try:
            _manager.auto_instrument(tracer_provider=provider, wire=False, lazy=False)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=install)
    worker.start()
    try:
        assert entered.wait(3)
        provider.add_span_processor(app_processor)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    _manager.shutdown()
    assert provider._active_span_processor._span_processors == (app_processor,)
    assert not app.closed
    with provider.get_tracer("app").start_as_current_span("survives"):
        pass
    assert only(app).name == "survives"
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("rate", [0.0, 1.0])
@pytest.mark.parametrize("app_sampler", [ALWAYS_OFF, ALWAYS_ON])
def test_agent_sampling_independent_even_when_app_drops(fresh_state, rate, app_sampler):
    from fi_instrumentation.auto._sampling import agent_provider

    provider = TracerProvider(sampler=app_sampler, shutdown_on_exit=False)
    app, fi = Exporter(), Exporter()
    provider.add_span_processor(SimpleSpanProcessor(app))
    trace.set_tracer_provider(provider)
    _manager.init(span_exporter=fi, batch=False, sample_rate=rate, instrumentations=["wire"])
    assert provider.sampler is app_sampler
    ordinary = provider.get_tracer("application")
    tracer = FITracer(provider.get_tracer("library"), config=TraceConfig())
    with ordinary.start_as_current_span("app-parent") as parent:
        assert parent.is_recording() == (app_sampler is ALWAYS_ON)
        for name, source in (("fi-library", tracer), ("native-library", agent_provider(provider).get_tracer("native"))):
            with source.start_as_current_span(name) as span:
                assert bool(span.get_span_context().trace_flags.sampled) == (app_sampler is ALWAYS_ON)
        finish(wire._recorder.begin("POST", URL, {}, json.dumps(CHAT).encode()))
    assert len(app.get_finished_spans()) == (4 if app_sampler is ALWAYS_ON else 0)
    assert len(fi.get_finished_spans()) == int(rate) * (4 if app_sampler is ALWAYS_ON else 3)
    for span in fi.get_finished_spans():
        assert span.context.trace_flags.sampled
    # The app still gets its exact original recording/sampling decision afterwards.
    after = ordinary.start_span("after")
    assert after.is_recording() == (app_sampler is ALWAYS_ON)
    after.end()
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("override", [{"wire": False}, {"instrumentations": ["fake"]}, {"disabled_instrumentations": ["fake"]}])
def test_supplied_settings_merge_only_explicit_overrides(fresh_state, override):
    settings = AutoConfig(instrumentations=[], disabled_instrumentations=["broken"],
                          wire_max_body_bytes=16, api_key="explicit-key", sdk_disabled=True,
                          sources={"wire_max_body_bytes": "argument"})
    # An explicit config must not accidentally read a newly created invalid file.
    (fresh_state / "futureagi.toml").write_text("invalid TOML")
    provider = TracerProvider(shutdown_on_exit=False)
    _manager.auto_instrument(tracer_provider=provider, settings=settings, **override)
    actual = _manager._state.settings
    assert actual.wire_max_body_bytes == 16 and actual.api_key == "explicit-key" and actual.sdk_disabled
    assert actual.instrumentations == override.get("instrumentations", [])
    assert actual.disabled_instrumentations == override.get("disabled_instrumentations", ["broken"])
    assert settings.instrumentations == [] and settings.disabled_instrumentations == ["broken"]
    _manager.shutdown()
    provider.shutdown()


def test_aiohttp_callback_cached_body_keeps_completion_and_usage(recording):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, tracer = recording
    wire.enable(tracer, transports=["aiohttp"])

    async def run(server):
        cached = []

        async def callback(response):
            cached.append(await response.read())

        async with aiohttp.ClientSession(raise_for_status=callback) as session:
            async with session.post(server.url + "/v1/chat/completions", json=CHAT) as response:
                assert await response.json() == CHAT_RESPONSE
                assert await response.read() == cached[0]

    with MockServer() as server:
        asyncio.run(run(server))
    attrs = only(exporter).attributes
    assert attrs["output.value"] == "pong" and attrs["gen_ai.usage.total_tokens"] == 6
    assert "traceai.wire.incomplete" not in attrs


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_httpx_stream_decoding_error_is_not_consumer_abort(recording, module, asynchronous):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording
    wire.enable(tracer, transports=[module])
    headers = {"content-type": "application/json", "content-encoding": "gzip"}

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"not-a-gzip-stream"

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"not-a-gzip-stream"

    if asynchronous:
        async def run():
            async def handle(request):
                return httpx.Response(200, headers=headers, stream=AsyncBody())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                async with client.stream("POST", URL, json=CHAT) as response:
                    with pytest.raises(httpx.DecodingError):
                        async for _ in response.aiter_bytes():
                            pass
        asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=SyncBody()))) as client:
            with client.stream("POST", URL, json=CHAT) as response:
                with pytest.raises(httpx.DecodingError):
                    list(response.iter_bytes())
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["error.type"] == "DecodingError"
    assert "traceai.wire.incomplete" not in span.attributes


@pytest.mark.parametrize("app_owned", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_agent_native_tracer_privacy_covers_all_destinations(fresh_state, app_owned, hidden):
    from fi_instrumentation.auto._sampling import agent_provider

    app, fi = Exporter(), Exporter()
    if app_owned:
        application = TracerProvider(shutdown_on_exit=False)
        trace.set_tracer_provider(application)
        application.add_span_processor(SimpleSpanProcessor(app))
    config = TraceConfig(hide_input_text=hidden, hide_output_text=hidden)
    provider = _manager.init(span_exporter=fi, batch=False, config=config, instrumentations=["wire"])
    if not app_owned:
        provider.add_span_processor(SimpleSpanProcessor(app))
    native = agent_provider(provider).get_tracer("native-library")
    secret, media = "REQUEST_CREDENTIAL_123", "AbCd0123" * 80
    payload = json.dumps({"authorization_token": "BODY_CREDENTIAL", "echo": secret,
                          "image": "data:image/png;base64," + media,
                          "url": "https://example.org/?sig=SIGNED_CREDENTIAL"})
    with native.start_as_current_span("native", attributes={"gen_ai.span.kind": "LLM"}) as span:
        assert wire._recorder.begin("POST", URL, {"Authorization": "Bearer " + secret}, json.dumps(CHAT).encode()) is None
        span.set_attributes({"gen_ai.input.messages": "PRIVATE_INPUT", "gen_ai.output.messages": "PRIVATE_OUTPUT",
                             "payload": payload})
        span.add_event("exception", {"exception.message": payload, "gen_ai.input.messages": "PRIVATE_INPUT"})
        span.set_status(trace.Status(trace.StatusCode.ERROR, payload))
    for destination in (app, fi):
        exported = only(destination)
        text = str(dict(exported.attributes)) + str(exported.status.description) + str([dict(e.attributes) for e in exported.events])
        for forbidden in (secret, media, "BODY_CREDENTIAL", "SIGNED_CREDENTIAL"):
            assert forbidden not in text
        if hidden:
            assert "PRIVATE_" not in text
            assert exported.status.description is None
            assert "exception.message" not in exported.events[0].attributes
    _manager.shutdown()
    provider.shutdown()


def test_invocation_hiding_covers_scalars_raw_json_and_events(fresh_state):
    attrs = {
        "gen_ai.request.parameters": '{"temperature":0.987,"max_tokens":123}',
        "gen_ai.request.temperature": 0.987, "gen_ai.request.max_tokens": 123,
        "gen_ai.request.custom": "PRIVATE_PARAMETER",
        "input.value": '{"messages":[],"temperature":0.987,"max_tokens":123}',
        "raw.input": '{"max_tokens":123,"parameter":"PRIVATE_PARAMETER"}',
    }
    provider = TracerProvider(shutdown_on_exit=False)
    app, fi = Exporter(), Exporter()
    provider.add_span_processor(SimpleSpanProcessor(app))
    trace.set_tracer_provider(provider)
    config = TraceConfig(hide_llm_invocation_parameters=True)
    _manager.init(span_exporter=fi, batch=False, config=config, instrumentations=[])
    with FITracer(provider.get_tracer("library"), config=config).start_as_current_span("library") as span:
        span.__wrapped__.set_attributes(attrs)
        span.__wrapped__.add_event("request", attrs)
    for destination in (app, fi):
        span = only(destination)
        for exported in [span.attributes] + [event.attributes for event in span.events]:
            assert not any(key.startswith("gen_ai.request.") for key in exported)
            assert exported["input.value"] == exported["raw.input"] == "__REDACTED__"
            assert "PRIVATE_PARAMETER" not in str(dict(exported))
    _manager.shutdown()
    provider.shutdown()
