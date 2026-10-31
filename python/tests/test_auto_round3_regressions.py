"""Event/lock-synchronized regressions for the ten independent round-three findings."""

import json
import sys
import threading
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager, _registry, wire
from fi_instrumentation.auto.wire import _core
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_safety import only  # noqa: F401

URL = "https://api.openai.com/v1/chat/completions"
CHAT = {"model": "gpt", "messages": [{"role": "user", "content": "PRIVATE_INPUT"}]}
RESPONSE = {"choices": [{"message": {"role": "assistant", "content": "PRIVATE_OUTPUT"}}]}


def finish(capture, response=RESPONSE):
    assert capture is not None
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(json.dumps(response).encode())
    capture.finish()


def app_provider():
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter


def test_brotli_without_hard_output_bound_is_never_invoked(monkeypatch):
    calls = []

    class UnsafeDecoder:
        def __init__(self):
            calls.append("constructed")

        def process(self, data):
            pytest.fail("unbounded Brotli expansion was invoked")

    for name in ("brotli", "brotlicffi"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(Decompressor=UnsafeDecoder))
    assert _core.decode_encoded(b"tiny-compressed-bomb", "br", 256) == (None, False)
    response = _core.ResponseData(status=200, headers={"content-encoding": "br", "content-type": "application/json"},
                                  body=b"tiny-compressed-bomb", max_body_bytes=256)
    assert _core.decode_response(response).kind == "empty"
    assert response.undecoded_encoding == "br"
    assert calls == []
    # Content already decoded by the transport is still parsed normally.
    response.body, response.decoded = b'{"choices":[]}', True
    assert _core.decode_response(response).json == {"choices": []}


@pytest.fixture
def base_integration(fresh_state, monkeypatch):
    class Instrumentor(BaseInstrumentor):
        entered = None
        release = None
        uninstallations = 0

        def instrumentation_dependencies(self):
            return ()

        def _instrument(self, **kwargs):
            self.provider = kwargs["tracer_provider"]

        def _uninstrument(self, **kwargs):
            if self.entered is not None:
                self.entered.set()
                assert self.release.wait(3)
            self.uninstallations += 1

    instrumentor = Instrumentor()
    item = _registry.Integration("owned", ("fakeailib",), "fake", "traceai_base_double", "Instrumentor")
    module = SimpleNamespace(Instrumentor=lambda: instrumentor,
                             SemanticKernelInstrumentor=lambda: instrumentor,
                             StrandsInstrumentor=lambda: instrumentor)
    monkeypatch.setitem(sys.modules, item.module, module)
    monkeypatch.setattr(_registry, "ALL", (item,))
    yield item, instrumentor
    instrumentor.entered = None
    instrumentor.uninstrument()


@pytest.mark.parametrize("name", ["semantic_kernel", "strands"])
def test_setup_adopts_active_base_instrumentor_without_cleanup(base_integration, monkeypatch, name):
    item, instrumentor = base_integration
    item = _registry.Integration(name, item.targets, item.package, item.module, name, kind=_registry.SETUP)
    monkeypatch.setattr(_registry, "ALL", (item,))
    provider = TracerProvider(shutdown_on_exit=False)
    instrumentor.instrument(tracer_provider=provider)
    _manager.auto_instrument(tracer_provider=provider, wire=False, lazy=False)
    assert {s.name: s.detail for s in _manager.report()}[name] == "already instrumented"
    _manager.auto_instrument(instrumentations=[], wire=False)
    _manager.shutdown()
    assert instrumentor.is_instrumented_by_opentelemetry
    assert instrumentor.uninstallations == 0
    provider.shutdown()


def test_otel_entry_uninstrument_stops_integrations_and_route(base_integration, monkeypatch):
    from fi_instrumentation.auto import _bootstrap
    from fi_instrumentation.auto._otel_entry import FutureAGIInstrumentor

    item, instrumentor = base_integration
    provider, app_exporter = app_provider()
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("FI_API_KEY", "CONFIGURED_API_KEY")
    monkeypatch.setattr(_bootstrap, "start", lambda: _manager.init(
        span_exporter=exporter, batch=False, instrumentations=[item.name, "wire"], lazy=False))
    entry = FutureAGIInstrumentor()
    entry.instrument()
    assert instrumentor.is_instrumented_by_opentelemetry
    entry.uninstrument()
    assert not entry.is_instrumented_by_opentelemetry
    assert not instrumentor.is_instrumented_by_opentelemetry
    assert not _manager._state.overlays[provider].enabled
    assert wire._recorder is None
    with provider.get_tracer("library").start_as_current_span("after-uninstrument"):
        pass
    assert not exporter.get_finished_spans()
    assert only(app_exporter).name == "after-uninstrument"
    provider.shutdown()


@pytest.mark.parametrize("rate", [0.0, 1.0])
def test_application_provider_fi_sampling_does_not_change_app_sampling(fresh_state, rate):
    provider, app_exporter = app_provider()
    exporter = InMemorySpanExporter()
    _manager.init(span_exporter=exporter, sample_rate=rate, batch=False, instrumentations=["wire"])
    assert provider.sampler is ALWAYS_ON
    finish(wire._recorder.begin("POST", URL, {}, json.dumps(CHAT).encode()))
    with provider.get_tracer("library").start_as_current_span("library-call"):
        pass
    assert len(app_exporter.get_finished_spans()) == 2
    assert len(exporter.get_finished_spans()) == int(rate) * 2
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("rate", [0.0, 1.0])
def test_fi_route_inherits_its_own_head_decision_not_app_sampled_bit(fresh_state, rate):
    provider, app_exporter = app_provider()
    exporter = InMemorySpanExporter()
    _manager.init(span_exporter=exporter, sample_rate=rate, batch=False, instrumentations=["wire"])
    tracer = provider.get_tracer("application")
    with tracer.start_as_current_span("app-root"):
        with tracer.start_as_current_span("framework-child"):
            finish(wire._recorder.begin("POST", URL, {}, json.dumps(CHAT).encode()))
    assert len(app_exporter.get_finished_spans()) == 3
    assert len(exporter.get_finished_spans()) == int(rate) * 3
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("sampled", [False, True])
def test_fi_route_sampling_honors_remote_parent(fresh_state, sampled):
    provider, app_exporter = app_provider()
    exporter = InMemorySpanExporter()
    _manager.init(span_exporter=exporter, sample_rate=0, batch=False, instrumentations=[])
    parent = trace.NonRecordingSpan(trace.SpanContext(
        0x12345678901234567890123456789012, 0x1234567890123456, is_remote=True,
        trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED if sampled else trace.TraceFlags.DEFAULT)))
    with provider.get_tracer("application").start_as_current_span("child", context=trace.set_span_in_context(parent)):
        pass
    assert len(app_exporter.get_finished_spans()) == 1
    assert len(exporter.get_finished_spans()) == int(sampled)
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("layer", ["wire", "library"])
def test_text_only_hiding_keeps_message_structure(fresh_state, layer):
    provider, app_exporter = app_provider()
    exporter = InMemorySpanExporter()
    config = TraceConfig(hide_input_text=True, hide_output_text=True)
    _manager.init(span_exporter=exporter, batch=False, config=config, instrumentations=["wire"])
    input_prefix = "gen_ai.input.messages.0.message"
    output_prefix = "gen_ai.output.messages.0.message"
    call_prefix = output_prefix + ".tool_calls.0.tool_call"
    if layer == "wire":
        request = {"model": "gpt", "messages": [{"role": "tool", "tool_call_id": "call-17", "content": "PRIVATE_INPUT"}]}
        response = {"choices": [{"message": {"role": "assistant", "content": "PRIVATE_OUTPUT", "tool_calls": [
            {"id": "call-17", "function": {"name": "weather", "arguments": '{"city":"PRIVATE_CITY"}'}}]}}]}
        finish(wire._recorder.begin("POST", URL, {}, json.dumps(request).encode()), response)
    else:
        with FITracer(provider.get_tracer("library"), config=config).start_as_current_span("library") as span:
            attrs = {input_prefix + ".role": "tool", input_prefix + ".tool_call_id": "call-17",
                     input_prefix + ".content": "PRIVATE_INPUT", output_prefix + ".role": "assistant",
                     output_prefix + ".content": "PRIVATE_OUTPUT", call_prefix + ".id": "call-17",
                     call_prefix + ".function.name": "weather", call_prefix + ".function.arguments": "PRIVATE_CITY"}
            span.__wrapped__.set_attributes(attrs)
            span.__wrapped__.add_event("message", attrs)
    for destination in (exporter, app_exporter):
        span = only(destination)
        for attrs in [span.attributes] + [e.attributes for e in span.events]:
            assert attrs[input_prefix + ".role"] == "tool"
            assert attrs[input_prefix + ".tool_call_id"] == "call-17"
            assert attrs[output_prefix + ".role"] == "assistant"
            assert attrs[call_prefix + ".id"] == "call-17"
            assert attrs[call_prefix + ".function.name"] == "weather"
            assert attrs[input_prefix + ".content"] == "__REDACTED__"
            assert attrs[output_prefix + ".content"] == "__REDACTED__"
            assert "PRIVATE_" not in str(dict(attrs))
    _manager.shutdown()
    provider.shutdown()


def test_normal_shutdown_still_flushes_queued_real_batch(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=True, schedule_delay_millis=60000,
                             max_export_batch_size=8, instrumentations=[])
    with provider.get_tracer("library").start_as_current_span("last-span"):
        pass
    _manager.shutdown(1000)
    assert only(exporter).name == "last-span"
