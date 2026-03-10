"""Unit tests for Milvus instrumentation."""

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
    resource = Resource(attributes={"service.name": "test-milvus"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    return tracer_provider


class TestMilvusInstrumentor:
    """Test MilvusInstrumentor class."""

    def test_instrumentor_can_be_instantiated(self):
        from traceai_milvus import MilvusInstrumentor
        instrumentor = MilvusInstrumentor()
        assert instrumentor is not None

    def test_instrumentation_dependencies(self):
        from traceai_milvus import MilvusInstrumentor
        instrumentor = MilvusInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert any("pymilvus" in dep for dep in deps)

    def test_instrumentor_instruments_without_error(self, tracer_provider):
        from traceai_milvus import MilvusInstrumentor

        instrumentor = MilvusInstrumentor()
        try:
            instrumentor.instrument(tracer_provider=tracer_provider)
            instrumentor.uninstrument()
        except ImportError:
            pass
