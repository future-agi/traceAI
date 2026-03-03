"""
E2E Tests for ChromaDB SDK Instrumentation

Tests ChromaDB instrumentation with in-memory client. No API keys needed.
"""

import pytest
import time

from config import config


@pytest.fixture(scope="module")
def chromadb_setup():
    """Set up ChromaDB with instrumentation."""
    from fi_instrumentation import register
    try:
        from traceai_chromadb import ChromaDBInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_chromadb not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    ChromaDBInstrumentor().instrument(tracer_provider=tracer_provider)

    try:
        import chromadb
    except ImportError:
        pytest.skip("chromadb not installed")

    client = chromadb.Client()

    yield client

    ChromaDBInstrumentor().uninstrument()


@pytest.fixture
def collection(chromadb_setup):
    """Create a fresh collection for each test."""
    import uuid

    collection_name = f"test_{uuid.uuid4().hex[:8]}"
    collection = chromadb_setup.create_collection(name=collection_name)
    yield collection
    chromadb_setup.delete_collection(name=collection_name)


class TestChromaDBOperations:
    """Test ChromaDB CRUD operations."""

    def test_add_and_query(self, collection):
        """Test adding documents and querying."""
        collection.add(
            ids=["1", "2", "3"],
            documents=[
                "The quick brown fox jumps over the lazy dog.",
                "Machine learning is a subset of artificial intelligence.",
                "Python is a popular programming language.",
            ],
        )

        results = collection.query(
            query_texts=["programming language"],
            n_results=1,
        )

        assert results["ids"][0][0] == "3"
        time.sleep(2)
        print(f"Query result: {results['ids']}")

    def test_add_with_embeddings(self, collection):
        """Test adding documents with pre-computed embeddings."""
        import random

        embeddings = [[random.uniform(-1, 1) for _ in range(128)] for _ in range(3)]

        collection.add(
            ids=["e1", "e2", "e3"],
            embeddings=embeddings,
            documents=["doc1", "doc2", "doc3"],
            metadatas=[{"type": "a"}, {"type": "b"}, {"type": "c"}],
        )

        results = collection.query(
            query_embeddings=[embeddings[0]],
            n_results=1,
        )

        assert results["ids"][0][0] == "e1"

    def test_upsert(self, collection):
        """Test upsert operation."""
        collection.add(
            ids=["u1"],
            documents=["original document"],
        )

        collection.upsert(
            ids=["u1"],
            documents=["updated document"],
        )

        result = collection.get(ids=["u1"])
        assert result["documents"][0] == "updated document"

    def test_delete(self, collection):
        """Test delete operation."""
        collection.add(
            ids=["d1", "d2", "d3"],
            documents=["one", "two", "three"],
        )

        collection.delete(ids=["d2"])

        result = collection.get()
        assert len(result["ids"]) == 2
        assert "d2" not in result["ids"]

    def test_get(self, collection):
        """Test get operation."""
        collection.add(
            ids=["g1", "g2"],
            documents=["hello", "world"],
            metadatas=[{"key": "a"}, {"key": "b"}],
        )

        result = collection.get(ids=["g1"])
        assert result["ids"] == ["g1"]
        assert result["documents"][0] == "hello"

    def test_get_with_where_filter(self, collection):
        """Test get with metadata filter."""
        collection.add(
            ids=["f1", "f2", "f3"],
            documents=["alpha", "beta", "gamma"],
            metadatas=[
                {"category": "vowel"},
                {"category": "consonant"},
                {"category": "consonant"},
            ],
        )

        result = collection.get(where={"category": "consonant"})
        assert len(result["ids"]) == 2

    def test_count(self, collection):
        """Test count operation."""
        collection.add(
            ids=["c1", "c2", "c3"],
            documents=["one", "two", "three"],
        )

        count = collection.count()
        assert count == 3

    def test_update(self, collection):
        """Test update operation."""
        collection.add(
            ids=["up1"],
            documents=["original"],
            metadatas=[{"version": "1"}],
        )

        collection.update(
            ids=["up1"],
            documents=["updated"],
            metadatas=[{"version": "2"}],
        )

        result = collection.get(ids=["up1"])
        assert result["documents"][0] == "updated"
        assert result["metadatas"][0]["version"] == "2"


class TestChromaDBCollectionManagement:
    """Test collection-level operations."""

    def test_create_and_list_collections(self, chromadb_setup):
        """Test collection creation and listing."""
        import uuid

        name = f"list_test_{uuid.uuid4().hex[:8]}"
        chromadb_setup.create_collection(name=name)

        collections = chromadb_setup.list_collections()
        assert any(c.name == name for c in collections)

        chromadb_setup.delete_collection(name=name)

    def test_get_or_create_collection(self, chromadb_setup):
        """Test get_or_create_collection."""
        import uuid

        name = f"goc_test_{uuid.uuid4().hex[:8]}"

        col1 = chromadb_setup.get_or_create_collection(name=name)
        col2 = chromadb_setup.get_or_create_collection(name=name)

        assert col1.name == col2.name

        chromadb_setup.delete_collection(name=name)
