import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
from traceai_agentscope import _lifecycle


@pytest.fixture(autouse=True)
def isolated_global(monkeypatch):
    # OTel global provider registration is once-only in production. Isolate tests.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(_lifecycle, "_slots", _lifecycle.WeakKeyDictionary())
    for name in (
        "FI_API_KEY",
        "FI_SECRET_KEY",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_SDK_DISABLED",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
        "OTEL_PYTHON_TRACER_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    yield
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()


@pytest.fixture
def shared():
    provider = TracerProvider(
        resource=Resource({"private.resource": "PRIVATE_RESOURCE"}),
        shutdown_on_exit=False,
    )
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    trace.set_tracer_provider(provider)
    return provider, raw
