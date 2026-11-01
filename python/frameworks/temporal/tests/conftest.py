import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
from test_wire import receiver  # noqa: F401 -- expose the loopback fixture to example tests

from traceai_temporal import configure


@pytest.fixture(autouse=True)
def isolated_otel(monkeypatch):
    # The OTel process-global registration is one-shot; each test models a fresh
    # process. These private test-only resets are never used by the integration.
    import os

    for key in list(os.environ):
        if key.startswith(("FI_", "OTEL_", "KEEP_TEMPORAL_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("FI_API_KEY", "synthetic-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret-key")
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    yield


@pytest.fixture
def tracing():
    handles = []

    def create(**kwargs):
        exporter = kwargs.pop("exporter", InMemorySpanExporter())
        handle = configure(
            project_name="synthetic-temporal", exporter=exporter, **kwargs
        )
        handles.append(handle)
        return handle, exporter

    yield create
    for handle in handles:
        assert handle.shutdown()
