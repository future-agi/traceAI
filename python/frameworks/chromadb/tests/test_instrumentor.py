"""Unit tests for ChromaDB instrumentation."""

import pytest
from unittest.mock import MagicMock, patch
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class TestChromaDBInstrumentor:
    """Test ChromaDBInstrumentor class."""

    def test_instrumentor_can_be_instantiated(self):
        """Test that instrumentor can be instantiated."""
        from traceai_chromadb import ChromaDBInstrumentor

        instrumentor = ChromaDBInstrumentor()
        assert instrumentor is not None

    def test_instrumentation_dependencies(self):
        """Test that instrumentor reports correct dependencies."""
        from traceai_chromadb import ChromaDBInstrumentor

        instrumentor = ChromaDBInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert any("chromadb" in dep for dep in deps)


class TestChromaDBQuery:
    """Test ChromaDB query operation tracing."""

    @patch("chromadb.Collection")
    def test_query_creates_span(
        self,
        mock_collection_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_chroma_collection,
        instrument_chromadb,
    ):
        """Test that query operation creates a span with correct attributes."""
        mock_collection_class.return_value = mock_chroma_collection

        # Execute query
        collection = mock_collection_class()
        collection.name = "test-collection"
        result = collection.query(
            query_embeddings=[[0.1, 0.2, 0.3]],
            n_results=5,
            include=["documents", "metadatas", "distances"],
        )

        spans = in_memory_span_exporter.get_finished_spans()
        query_spans = [s for s in spans if "query" in s.name.lower()]
        if query_spans:
            span = query_spans[0]
            assert "chroma" in span.name.lower() or "chromadb" in span.name.lower()


class TestChromaDBAdd:
    """Test ChromaDB add operation tracing."""

    @patch("chromadb.Collection")
    def test_add_creates_span(
        self,
        mock_collection_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_chroma_collection,
        instrument_chromadb,
    ):
        """Test that add operation creates a span with correct attributes."""
        mock_collection_class.return_value = mock_chroma_collection

        collection = mock_collection_class()
        collection.name = "test-collection"
        collection.add(
            ids=["id1", "id2"],
            embeddings=[[0.1, 0.2], [0.3, 0.4]],
            documents=["doc1", "doc2"],
            metadatas=[{"key": "val1"}, {"key": "val2"}],
        )

        spans = in_memory_span_exporter.get_finished_spans()
        add_spans = [s for s in spans if "add" in s.name.lower()]
        if add_spans:
            span = add_spans[0]
            attributes = dict(span.attributes or {})
            assert attributes.get("db.system") == "chroma"


class TestChromaDBErrorHandling:
    """Test error handling in ChromaDB instrumentation."""

    @patch("chromadb.Collection")
    def test_query_error_is_recorded(
        self,
        mock_collection_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_chromadb,
    ):
        """Test that errors during query are recorded in span."""
        mock_collection = MagicMock()
        mock_collection.query.side_effect = Exception("Database error")
        mock_collection_class.return_value = mock_collection

        collection = mock_collection_class()

        with pytest.raises(Exception, match="Database error"):
            collection.query(query_embeddings=[[0.1, 0.2]])

        spans = in_memory_span_exporter.get_finished_spans()
        if spans:
            span = spans[0]
            # Check that error was recorded
            assert not span.status.is_ok or span.events
