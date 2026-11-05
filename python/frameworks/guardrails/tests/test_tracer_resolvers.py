"""The native SDK imports both an OTel factory and an optional-tracer resolver."""

import asyncio
from importlib.util import find_spec

import pytest
from fi_instrumentation import TraceConfig
from guardrails import AsyncGuard, Guard
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_guardrails import GuardrailsInstrumentor


@pytest.fixture
def telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".guardrailsrc").write_text("no_metrics=true\n")
    from guardrails.settings import settings

    if hasattr(settings, "rc"):
        monkeypatch.setattr(settings.rc, "enable_metrics", False)
        monkeypatch.setattr(Guard, "_load_rc", lambda self: None)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GuardrailsInstrumentor()
    yield provider, exporter, instrumentor
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_supported_validation_with_uninstrumented_control(telemetry, enabled, asynchronous):
    provider, exporter, instrumentor = telemetry
    if enabled:
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True)
        )
    guard = (AsyncGuard if asynchronous else Guard)()
    guard.configure(allow_metrics_collection=False)
    with provider.get_tracer("application").start_as_current_span("application") as parent:
        result = guard.validate("PRIVATE_VALIDATION")
        if asynchronous:
            result = asyncio.run(result)
        assert result.validated_output == "PRIVATE_VALIDATION"
        assert trace.get_current_span() is parent
    children = [span for span in exporter.get_finished_spans() if span.name != "application"]
    assert bool(children) is enabled
    assert b"PRIVATE_VALIDATION" not in encode_spans(exporter.get_finished_spans()).SerializeToString()


def test_optional_tracer_resolution_and_factory_restore(telemetry):
    if find_spec("guardrails.telemetry.guard_tracing") is None:
        pytest.skip("Native tracer binding")
    from guardrails.telemetry import guard_tracing, runner_tracing, validator_tracing

    if not hasattr(runner_tracing, "get_tracer"):
        pytest.skip("This SDK uses only the OTel factory")
    from guardrails.stores.context import get_tracer, set_tracer

    provider, exporter, instrumentor = telemetry
    originals = [(module, module.get_tracer) for module in (guard_tracing, runner_tracing, validator_tracing)]
    original_context_tracer = get_tracer()
    global_factory = trace.get_tracer
    try:
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True)
        )
        set_tracer(None)
        assert runner_tracing.get_tracer() is None
        explicit = provider.get_tracer("explicit")
        set_tracer(explicit)
        for resolver in (runner_tracing.get_tracer, validator_tracing.get_tracer):
            for tracer in (resolver(), resolver(explicit), resolver(tracer=explicit)):
                with tracer.start_as_current_span("guard") as span:
                    span.set_attribute("input.value", "PRIVATE_RESOLVER")
                    span.set_attribute("output.value", "PRIVATE_RESOLVER")
        with guard_tracing.get_tracer("factory-scope").start_as_current_span("guard") as span:
            span.set_attribute("input.value", "PRIVATE_FACTORY")
        assert len(exporter.get_finished_spans()) == 7
        assert b"PRIVATE_" not in encode_spans(exporter.get_finished_spans()).SerializeToString()
        assert trace.get_tracer is global_factory
    finally:
        set_tracer(original_context_tracer)
        instrumentor.uninstrument()
    for module, original in originals:
        assert module.get_tracer is original
