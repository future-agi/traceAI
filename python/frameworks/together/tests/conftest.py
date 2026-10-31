"""Offline fixtures using the installed core and OpenTelemetry packages."""

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """These unit/serialization contracts must never contact a provider."""
    import socket

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def loopback_only(original):
        def connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("External network calls are forbidden in provider tests")
            return original(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", loopback_only(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", loopback_only(original_connect_ex))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")


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
