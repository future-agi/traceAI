"""Unit tests for Weaviate instrumentation."""

import pytest
from unittest.mock import MagicMock, patch
from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def in_memory_span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(in_memory_span_exporter):
    resource = Resource(attributes={"service.name": "test-weaviate"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    return tracer_provider


class TestWeaviateInstrumentor:
    """Test WeaviateInstrumentor class."""

    def test_instrumentor_can_be_instantiated(self):
        from traceai_weaviate import WeaviateInstrumentor
        instrumentor = WeaviateInstrumentor()
        assert instrumentor is not None

    def test_instrumentation_dependencies(self):
        from traceai_weaviate import WeaviateInstrumentor
        instrumentor = WeaviateInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert any("weaviate" in dep for dep in deps)


class TestWeaviateOperations:
    """Test Weaviate operation tracing."""

    def test_instrumentor_instruments_without_error(self, tracer_provider, in_memory_span_exporter):
        """Test that instrumentor can instrument without errors."""
        from traceai_weaviate import WeaviateInstrumentor

        instrumentor = WeaviateInstrumentor()
        # Should not raise even if weaviate is not installed
        try:
            instrumentor.instrument(tracer_provider=tracer_provider)
            instrumentor.uninstrument()
        except ImportError:
            # Expected if weaviate not installed
            pass
