"""
E2E Tests for LanceDB SDK Instrumentation

Tests LanceDB instrumentation with local filesystem. No API keys needed.
"""

import pytest
import time
import tempfile
import shutil

from config import config


@pytest.fixture(scope="module")
def lancedb_setup():
    """Set up LanceDB with instrumentation."""
    from fi_instrumentation import register
    try:
        from traceai_lancedb import LanceDBInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_lancedb not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    LanceDBInstrumentor().instrument(tracer_provider=tracer_provider)

    # Create temp directory for LanceDB
    tmp_dir = tempfile.mkdtemp(prefix="lancedb_e2e_")

    try:
        import lancedb
    except ImportError:
        pytest.skip("lancedb not installed")

    db = lancedb.connect(tmp_dir)

    yield db

    LanceDBInstrumentor().uninstrument()
    shutil.rmtree(tmp_dir, ignore_errors=True)


class TestLanceDBOperations:
    """Test LanceDB CRUD operations."""

    def test_create_table_and_search(self, lancedb_setup):
        """Test creating a table and searching."""
        import random

        data = [
            {"id": i, "text": f"document {i}", "vector": [random.uniform(-1, 1) for _ in range(128)]}
            for i in range(10)
        ]

        table = lancedb_setup.create_table("test_search", data, mode="overwrite")

        results = table.search(data[0]["vector"]).limit(3).to_list()

        assert len(results) == 3
        assert results[0]["id"] == 0  # Closest to itself
        time.sleep(2)
        print(f"Search results: {[r['id'] for r in results]}")

    def test_add_data(self, lancedb_setup):
        """Test adding data to existing table."""
        import random

        initial_data = [
            {"id": i, "text": f"initial {i}", "vector": [random.uniform(-1, 1) for _ in range(128)]}
            for i in range(5)
        ]

        table = lancedb_setup.create_table("test_add", initial_data, mode="overwrite")

        new_data = [
            {"id": i + 5, "text": f"new {i}", "vector": [random.uniform(-1, 1) for _ in range(128)]}
            for i in range(3)
        ]

        table.add(new_data)

        assert len(table) == 8

    def test_delete(self, lancedb_setup):
        """Test deleting records."""
        import random

        data = [
            {"id": i, "text": f"doc {i}", "vector": [random.uniform(-1, 1) for _ in range(128)]}
            for i in range(5)
        ]

        table = lancedb_setup.create_table("test_delete", data, mode="overwrite")
        initial_count = len(table)

        table.delete("id = 2")

        assert len(table) == initial_count - 1

    def test_list_tables(self, lancedb_setup):
        """Test listing tables."""
        import random

        data = [
            {"id": 0, "text": "test", "vector": [random.uniform(-1, 1) for _ in range(128)]}
        ]
        lancedb_setup.create_table("list_test_table", data, mode="overwrite")

        tables = lancedb_setup.table_names()
        assert "list_test_table" in tables

    def test_search_with_filter(self, lancedb_setup):
        """Test search with filter."""
        import random

        data = [
            {
                "id": i,
                "text": f"doc {i}",
                "category": "A" if i < 5 else "B",
                "vector": [random.uniform(-1, 1) for _ in range(128)],
            }
            for i in range(10)
        ]

        table = lancedb_setup.create_table("test_filter", data, mode="overwrite")

        results = (
            table.search(data[0]["vector"])
            .where("category = 'A'")
            .limit(3)
            .to_list()
        )

        assert len(results) <= 3
        assert all(r["category"] == "A" for r in results)
