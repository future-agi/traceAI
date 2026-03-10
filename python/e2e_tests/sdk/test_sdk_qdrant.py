"""
E2E Tests for Qdrant SDK Instrumentation

Tests Qdrant instrumentation with in-memory client. No API keys needed.
"""

import pytest
import time
import uuid

from config import config


@pytest.fixture(scope="module")
def qdrant_setup():
    """Set up Qdrant with instrumentation."""
    from fi_instrumentation import register
    try:
        from traceai_qdrant import QdrantInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_qdrant not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    QdrantInstrumentor().instrument(tracer_provider=tracer_provider)

    try:
        from qdrant_client import QdrantClient
    except ImportError:
        pytest.skip("qdrant-client not installed")

    client = QdrantClient(":memory:")

    yield client

    QdrantInstrumentor().uninstrument()


@pytest.fixture
def collection_name():
    """Generate unique collection name."""
    return f"test_{uuid.uuid4().hex[:8]}"


class TestQdrantOperations:
    """Test Qdrant CRUD operations."""

    def test_create_collection_and_search(self, qdrant_setup, collection_name):
        """Test creating a collection, upserting points, and searching."""
        import random
        from qdrant_client.models import Distance, VectorParams, PointStruct

        qdrant_setup.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        points = [
            PointStruct(
                id=i,
                vector=[random.uniform(-1, 1) for _ in range(128)],
                payload={"text": f"document {i}", "category": "A" if i < 5 else "B"},
            )
            for i in range(10)
        ]

        qdrant_setup.upsert(
            collection_name=collection_name,
            points=points,
        )

        results = qdrant_setup.query_points(
            collection_name=collection_name,
            query=points[0].vector,
            limit=3,
        )

        assert len(results.points) == 3
        assert results.points[0].id == 0  # Closest to itself
        time.sleep(2)
        print(f"Search results: {[r.id for r in results.points]}")

    def test_upsert_and_get(self, qdrant_setup, collection_name):
        """Test upsert and get by ID."""
        import random
        from qdrant_client.models import Distance, VectorParams, PointStruct

        qdrant_setup.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        points = [
            PointStruct(
                id=1,
                vector=[random.uniform(-1, 1) for _ in range(128)],
                payload={"text": "hello world"},
            )
        ]

        qdrant_setup.upsert(collection_name=collection_name, points=points)

        result = qdrant_setup.retrieve(
            collection_name=collection_name,
            ids=[1],
        )

        assert len(result) == 1
        assert result[0].payload["text"] == "hello world"

    def test_delete_points(self, qdrant_setup, collection_name):
        """Test deleting points."""
        import random
        from qdrant_client.models import Distance, VectorParams, PointStruct, PointIdsList

        qdrant_setup.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        points = [
            PointStruct(
                id=i,
                vector=[random.uniform(-1, 1) for _ in range(128)],
                payload={"text": f"doc {i}"},
            )
            for i in range(5)
        ]

        qdrant_setup.upsert(collection_name=collection_name, points=points)

        qdrant_setup.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=[2, 3]),
        )

        info = qdrant_setup.get_collection(collection_name=collection_name)
        assert info.points_count == 3

    def test_search_with_filter(self, qdrant_setup, collection_name):
        """Test search with payload filter."""
        import random
        from qdrant_client.models import (
            Distance,
            VectorParams,
            PointStruct,
            Filter,
            FieldCondition,
            MatchValue,
        )

        qdrant_setup.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        points = [
            PointStruct(
                id=i,
                vector=[random.uniform(-1, 1) for _ in range(128)],
                payload={"category": "A" if i < 5 else "B"},
            )
            for i in range(10)
        ]

        qdrant_setup.upsert(collection_name=collection_name, points=points)

        results = qdrant_setup.query_points(
            collection_name=collection_name,
            query=points[0].vector,
            query_filter=Filter(
                must=[FieldCondition(key="category", match=MatchValue(value="A"))]
            ),
            limit=10,
        )

        assert all(r.payload["category"] == "A" for r in results.points)

    def test_scroll_points(self, qdrant_setup, collection_name):
        """Test scrolling through points."""
        import random
        from qdrant_client.models import Distance, VectorParams, PointStruct

        qdrant_setup.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        points = [
            PointStruct(
                id=i,
                vector=[random.uniform(-1, 1) for _ in range(128)],
                payload={"text": f"doc {i}"},
            )
            for i in range(10)
        ]

        qdrant_setup.upsert(collection_name=collection_name, points=points)

        records, _next_page = qdrant_setup.scroll(
            collection_name=collection_name,
            limit=5,
        )

        assert len(records) == 5


class TestQdrantCollectionManagement:
    """Test collection-level operations."""

    def test_list_collections(self, qdrant_setup):
        """Test listing collections."""
        from qdrant_client.models import Distance, VectorParams

        name = f"list_{uuid.uuid4().hex[:8]}"
        qdrant_setup.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        collections = qdrant_setup.get_collections().collections
        assert any(c.name == name for c in collections)

        qdrant_setup.delete_collection(collection_name=name)

    def test_collection_info(self, qdrant_setup, collection_name):
        """Test getting collection info."""
        from qdrant_client.models import Distance, VectorParams

        qdrant_setup.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=128, distance=Distance.COSINE),
        )

        info = qdrant_setup.get_collection(collection_name=collection_name)
        assert info.config.params.vectors.size == 128
