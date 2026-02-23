"""
End-to-End Integration Tests for LanceDB Instrumentation

LanceDB is embedded and doesn't require external setup.

Run with: pytest tests_e2e/test_lancedb_e2e.py -v
"""

import pytest
import tempfile
import shutil
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from conftest import generate_test_embedding


@pytest.fixture
def lancedb_connection():
    """Create temporary LanceDB connection."""
    try:
        import lancedb
        temp_dir = tempfile.mkdtemp()
        db = lancedb.connect(temp_dir)
        yield db
        shutil.rmtree(temp_dir, ignore_errors=True)
    except ImportError:
        pytest.skip("lancedb not installed")


@pytest.fixture
def instrument_lancedb(tracer_provider, in_memory_span_exporter):
    """Instrument LanceDB for testing."""
    from traceai_lancedb import LanceDBInstrumentor

    instrumentor = LanceDBInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield
    instrumentor.uninstrument()
    in_memory_span_exporter.clear()


class TestLanceDBE2E:
    """End-to-end tests for LanceDB instrumentation."""

    @pytest.mark.integration
    @pytest.mark.lancedb
    def test_full_workflow(
        self,
        lancedb_connection,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_lancedb,
    ):
        """Test complete workflow: create table, add, search."""
        db = lancedb_connection

        # Create table with initial data
        data = [
            {
                "id": i,
                "text": f"Document {i}",
                "vector": generate_test_embedding(f"Document {i}"),
            }
            for i in range(5)
        ]

        table = db.create_table("test_docs", data)

        # Search
        query_vector = generate_test_embedding("Document 1")
        results = table.search(query_vector).limit(3).to_list()

        # Verify results
        assert len(results) == 3

        # Check spans
        spans = in_memory_span_exporter.get_finished_spans()
        span_names = [s.name for s in spans]

        # Should have create_table and search spans
        assert any("create" in name.lower() for name in span_names)
        assert any("search" in name.lower() for name in span_names)

    @pytest.mark.integration
    @pytest.mark.lancedb
    def test_add_and_delete(
        self,
        lancedb_connection,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        instrument_lancedb,
    ):
        """Test add and delete operations."""
        db = lancedb_connection

        # Create table
        initial_data = [
            {"id": 0, "text": "Initial doc", "vector": generate_test_embedding("Initial")}
        ]
        table = db.create_table("add_delete_test", initial_data)

        # Add more data
        new_data = [
            {"id": 1, "text": "New doc 1", "vector": generate_test_embedding("New 1")},
            {"id": 2, "text": "New doc 2", "vector": generate_test_embedding("New 2")},
        ]
        table.add(new_data)

        # Delete
        table.delete("id = 1")

        # Check spans
        spans = in_memory_span_exporter.get_finished_spans()
        span_names = [s.name for s in spans]

        assert any("add" in name.lower() for name in span_names)
        assert any("delete" in name.lower() for name in span_names)
