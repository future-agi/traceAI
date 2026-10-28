"""Offline fixtures using the installed core and OpenTelemetry packages."""

from dataclasses import fields

import pytest
from fi_instrumentation import TraceConfig


@pytest.fixture(autouse=True)
def clean_privacy_environment(monkeypatch):
    """Each test opts into its policy instead of inheriting a developer's flags."""
    for field in fields(TraceConfig):
        monkeypatch.delenv(field.metadata["env_var"], raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """These unit/serialization contracts must never contact a provider."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in provider unit tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def export_attributes():
    """Exercise the real FITracer privacy boundary, not a mocked constants table."""
    from fi_instrumentation import FITracer, TraceConfig
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def export(attributes, **privacy):
        exporter.clear()
        tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(**privacy))
        with tracer.start_as_current_span("provider-contract") as span:
            span.set_attributes(attributes)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return dict(spans[0].attributes)

    yield export
    provider.shutdown()
