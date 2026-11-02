"""Regression tests with real Agno/OpenInference and a fake model transport."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from traceai_agno import AgnoInstrumentorWrapper, configure_agno_tracing


def test_explicit_provider_works_with_existing_global(tracing, agent, monkeypatch):
    provider, exporter = tracing
    unrelated = TracerProvider()
    from opentelemetry.util._once import Once

    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    trace.set_tracer_provider(unrelated)
    try:
        wrapper = configure_agno_tracing(tracer_provider=provider)
        assert wrapper.is_instrumented
        assert agent.run("What is the capital of France?").content == "Paris"
        spans = exporter.get_finished_spans()
        assert spans, "Explicit provider received no real Agno spans"
        assert trace.get_tracer_provider() is unrelated
        assert {s.attributes.get("openinference.span.kind") for s in spans} >= {
            "AGENT",
            "LLM",
        }
    finally:
        unrelated.shutdown()


def test_repeated_configuration_preserves_delegate(tracing, agent):
    provider, exporter = tracing
    wrapper = configure_agno_tracing(tracer_provider=provider)
    delegate = wrapper._agno_instrumentor
    assert configure_agno_tracing(tracer_provider=provider) is wrapper
    assert wrapper._agno_instrumentor is delegate
    wrapper.uninstrument()
    agent.run("What is the capital of France?")
    assert not exporter.get_finished_spans(), (
        "Repeated setup lost the delegate needed for teardown"
    )


def test_missing_instrumentor_is_not_reported_as_success(tracing, monkeypatch):
    import sys

    provider, _ = tracing
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.agno", None)
    wrapper = AgnoInstrumentorWrapper()
    with pytest.raises(ImportError, match="openinference-instrumentation-agno"):
        wrapper.instrument(tracer_provider=provider)
    assert not wrapper.is_instrumented


def test_endpoint_only_configuration_exports(tracing, agent, receiver, monkeypatch):
    endpoint, requests = receiver
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    wrapper = configure_agno_tracing(
        otlp_endpoint=endpoint,
        otlp_headers={"X-Api-Key": "synthetic-api", "X-Secret-Key": "synthetic-secret"},
        project_name="agno-test",
    )
    try:
        agent.run("What is the capital of France?")
        assert isinstance(wrapper._tracer_provider, TracerProvider)
        assert wrapper._tracer_provider.force_flush()
        assert requests, "Endpoint-only configuration exported no spans"
    finally:
        wrapper.uninstrument()
