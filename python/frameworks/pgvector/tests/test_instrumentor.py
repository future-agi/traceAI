"""Unit tests for pgvector instrumentation."""

import pytest
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
    resource = Resource(attributes={"service.name": "test-pgvector"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    return tracer_provider


class TestPgVectorInstrumentor:
    """Test PgVectorInstrumentor class."""

    def test_instrumentor_can_be_instantiated(self):
        from traceai_pgvector import PgVectorInstrumentor
        instrumentor = PgVectorInstrumentor()
        assert instrumentor is not None

    def test_instrumentation_dependencies(self):
        from traceai_pgvector import PgVectorInstrumentor
        instrumentor = PgVectorInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert any("pgvector" in dep for dep in deps)

    def test_instrumentor_instruments_without_error(self, tracer_provider):
        from traceai_pgvector import PgVectorInstrumentor

        instrumentor = PgVectorInstrumentor()
        try:
            instrumentor.instrument(tracer_provider=tracer_provider)
            instrumentor.uninstrument()
        except ImportError:
            pass


class TestVectorOperationDetection:
    """Test vector operation detection logic."""

    def test_detect_l2_distance(self):
        from traceai_pgvector._wrappers import detect_vector_operation

        query = "SELECT * FROM items ORDER BY embedding <-> '[1,2,3]' LIMIT 5"
        result = detect_vector_operation(query)

        assert result is not None
        distance_type, metadata = result
        assert distance_type == "l2_distance"
        assert metadata["db.vector.operator"] == "<->"

    def test_detect_cosine_distance(self):
        from traceai_pgvector._wrappers import detect_vector_operation

        query = "SELECT * FROM items ORDER BY embedding <=> '[1,2,3]' LIMIT 10"
        result = detect_vector_operation(query)

        assert result is not None
        distance_type, metadata = result
        assert distance_type == "cosine_distance"

    def test_detect_inner_product(self):
        from traceai_pgvector._wrappers import detect_vector_operation

        query = "SELECT * FROM items ORDER BY embedding <#> '[1,2,3]'"
        result = detect_vector_operation(query)

        assert result is not None
        distance_type, metadata = result
        assert distance_type == "inner_product"

    def test_non_vector_query_returns_none(self):
        from traceai_pgvector._wrappers import detect_vector_operation

        query = "SELECT * FROM users WHERE id = 1"
        result = detect_vector_operation(query)

        assert result is None

    def test_extract_limit(self):
        from traceai_pgvector._wrappers import detect_vector_operation

        query = "SELECT * FROM items ORDER BY embedding <-> '[1,2,3]' LIMIT 5"
        result = detect_vector_operation(query)

        assert result is not None
        _, metadata = result
        assert metadata.get("db.vector.query.top_k") == 5

    def test_extract_table_name(self):
        from traceai_pgvector._wrappers import detect_vector_operation

        query = "SELECT * FROM documents ORDER BY embedding <-> '[1,2,3]'"
        result = detect_vector_operation(query)

        assert result is not None
        _, metadata = result
        assert metadata.get("db.vector.collection.name") == "documents"
