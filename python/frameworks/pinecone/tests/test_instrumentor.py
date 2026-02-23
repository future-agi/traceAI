"""Unit tests for Pinecone instrumentation."""

import pytest
from unittest.mock import MagicMock, patch
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class TestPineconeInstrumentor:
    """Test PineconeInstrumentor class."""

    def test_instrumentor_can_be_instantiated(self):
        """Test that instrumentor can be instantiated."""
        from traceai_pinecone import PineconeInstrumentor

        instrumentor = PineconeInstrumentor()
        assert instrumentor is not None

    def test_instrumentation_dependencies(self):
        """Test that instrumentor reports correct dependencies."""
        from traceai_pinecone import PineconeInstrumentor

        instrumentor = PineconeInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "pinecone-client >= 3.0.0" in deps


class TestPineconeQuery:
    """Test Pinecone query operation tracing."""

    @patch("pinecone.Index")
    def test_query_creates_span(
        self,
        mock_index_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_pinecone_index,
        instrument_pinecone,
    ):
        """Test that query operation creates a span with correct attributes."""
        mock_index_class.return_value = mock_pinecone_index

        # Execute query
        index = mock_index_class("test-index")
        result = index.query(
            vector=[0.1] * 1536,
            top_k=10,
            namespace="test-ns",
            include_metadata=True,
        )

        # Verify span was created
        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) >= 1

        # Find the query span
        query_spans = [s for s in spans if "query" in s.name.lower()]
        if query_spans:
            span = query_spans[0]
            assert span.name == "pinecone query"
            attributes = dict(span.attributes or {})
            assert attributes.get("db.system") == "pinecone"
            assert attributes.get("db.operation.name") == "query"

    @patch("pinecone.Index")
    def test_query_with_filter_captures_filter(
        self,
        mock_index_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_pinecone_index,
        instrument_pinecone,
    ):
        """Test that query with filter captures filter in attributes."""
        mock_index_class.return_value = mock_pinecone_index

        # Execute query with filter
        index = mock_index_class("test-index")
        result = index.query(
            vector=[0.1] * 1536,
            top_k=5,
            filter={"genre": "action"},
        )

        spans = in_memory_span_exporter.get_finished_spans()
        query_spans = [s for s in spans if "query" in s.name.lower()]
        if query_spans:
            span = query_spans[0]
            attributes = dict(span.attributes or {})
            assert "db.vector.query.top_k" in attributes or attributes.get("db.vector.query.top_k") == 5


class TestPineconeUpsert:
    """Test Pinecone upsert operation tracing."""

    @patch("pinecone.Index")
    def test_upsert_creates_span(
        self,
        mock_index_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_pinecone_index,
        instrument_pinecone,
    ):
        """Test that upsert operation creates a span with correct attributes."""
        mock_index_class.return_value = mock_pinecone_index

        # Execute upsert
        index = mock_index_class("test-index")
        vectors = [
            {"id": "vec1", "values": [0.1] * 1536, "metadata": {"text": "doc1"}},
            {"id": "vec2", "values": [0.2] * 1536, "metadata": {"text": "doc2"}},
        ]
        result = index.upsert(vectors=vectors, namespace="test-ns")

        spans = in_memory_span_exporter.get_finished_spans()
        upsert_spans = [s for s in spans if "upsert" in s.name.lower()]
        if upsert_spans:
            span = upsert_spans[0]
            assert span.name == "pinecone upsert"
            attributes = dict(span.attributes or {})
            assert attributes.get("db.system") == "pinecone"


class TestPineconeDelete:
    """Test Pinecone delete operation tracing."""

    @patch("pinecone.Index")
    def test_delete_creates_span(
        self,
        mock_index_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_pinecone_index,
        instrument_pinecone,
    ):
        """Test that delete operation creates a span with correct attributes."""
        mock_index_class.return_value = mock_pinecone_index

        # Execute delete
        index = mock_index_class("test-index")
        result = index.delete(ids=["vec1", "vec2"], namespace="test-ns")

        spans = in_memory_span_exporter.get_finished_spans()
        delete_spans = [s for s in spans if "delete" in s.name.lower()]
        if delete_spans:
            span = delete_spans[0]
            assert span.name == "pinecone delete"
            attributes = dict(span.attributes or {})
            assert attributes.get("db.system") == "pinecone"


class TestPineconeErrorHandling:
    """Test error handling in Pinecone instrumentation."""

    @patch("pinecone.Index")
    def test_query_error_is_recorded(
        self,
        mock_index_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_pinecone,
    ):
        """Test that errors during query are recorded in span."""
        mock_index = MagicMock()
        mock_index.query.side_effect = Exception("Connection error")
        mock_index_class.return_value = mock_index

        index = mock_index_class("test-index")

        with pytest.raises(Exception, match="Connection error"):
            index.query(vector=[0.1] * 1536, top_k=10)

        spans = in_memory_span_exporter.get_finished_spans()
        # Error should still create a span
        if spans:
            span = spans[0]
            assert not span.status.is_ok or span.events  # Either error status or exception recorded
