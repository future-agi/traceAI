"""
End-to-End Integration Tests for ChromaDB Instrumentation

ChromaDB can run in ephemeral mode, so these tests don't require external setup.

Run with: pytest tests_e2e/test_chromadb_e2e.py -v
"""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def chromadb_client():
    """Create ephemeral ChromaDB client."""
    try:
        import chromadb
        return chromadb.Client()
    except ImportError:
        pytest.skip("chromadb not installed")


@pytest.fixture
def instrument_chromadb(tracer_provider, in_memory_span_exporter):
    """Instrument ChromaDB for testing."""
    from traceai_chromadb import ChromaDBInstrumentor

    instrumentor = ChromaDBInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield
    instrumentor.uninstrument()
    in_memory_span_exporter.clear()


class TestChromaDBE2E:
    """End-to-end tests for ChromaDB instrumentation."""

    @pytest.mark.integration
    @pytest.mark.chromadb
    def test_full_workflow(
        self,
        chromadb_client,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_chromadb,
    ):
        """Test complete workflow: create, add, query, delete."""
        # Create collection
        collection = chromadb_client.get_or_create_collection(
            name="test_collection",
            metadata={"hnsw:space": "cosine"}
        )

        # Add documents
        collection.add(
            ids=["doc1", "doc2", "doc3"],
            documents=[
                "Machine learning is a subset of AI.",
                "Deep learning uses neural networks.",
                "Natural language processing analyzes text.",
            ],
            metadatas=[
                {"category": "ml"},
                {"category": "dl"},
                {"category": "nlp"},
            ],
        )

        # Query
        results = collection.query(
            query_texts=["neural networks"],
            n_results=2,
            include=["documents", "metadatas", "distances"],
        )

        # Verify results
        assert results is not None
        assert len(results["ids"][0]) == 2

        # Get spans
        spans = in_memory_span_exporter.get_finished_spans()

        # Should have spans for add and query operations
        span_names = [s.name for s in spans]
        assert any("add" in name.lower() for name in span_names), f"No add span found in {span_names}"
        assert any("query" in name.lower() for name in span_names), f"No query span found in {span_names}"

        # Verify span attributes
        for span in spans:
            if "query" in span.name.lower():
                attrs = dict(span.attributes or {})
                assert attrs.get("db.system") == "chroma"
                break

        # Cleanup
        chromadb_client.delete_collection("test_collection")

    @pytest.mark.integration
    @pytest.mark.chromadb
    def test_error_handling(
        self,
        chromadb_client,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_chromadb,
    ):
        """Test that errors are properly captured in spans."""
        collection = chromadb_client.get_or_create_collection("error_test")

        # Try to add duplicate IDs (should succeed in ChromaDB)
        collection.add(
            ids=["dup1"],
            documents=["First document"],
        )

        # Adding same ID again should work (upsert behavior)
        collection.add(
            ids=["dup1"],
            documents=["Updated document"],
        )

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) >= 2

        # All spans should be OK
        for span in spans:
            assert span.status.is_ok

        chromadb_client.delete_collection("error_test")

    @pytest.mark.integration
    @pytest.mark.chromadb
    def test_batch_operations(
        self,
        chromadb_client,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_chromadb,
    ):
        """Test batch operations create proper spans."""
        collection = chromadb_client.get_or_create_collection("batch_test")

        # Batch add
        ids = [f"doc_{i}" for i in range(100)]
        documents = [f"Document number {i}" for i in range(100)]

        collection.add(ids=ids, documents=documents)

        # Verify count
        count = collection.count()
        assert count == 100

        # Check spans
        spans = in_memory_span_exporter.get_finished_spans()
        add_spans = [s for s in spans if "add" in s.name.lower()]

        assert len(add_spans) >= 1
        for span in add_spans:
            attrs = dict(span.attributes or {})
            # Should capture upsert count
            if "db.vector.upsert.count" in attrs:
                assert attrs["db.vector.upsert.count"] == 100

        chromadb_client.delete_collection("batch_test")
