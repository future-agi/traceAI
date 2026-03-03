"""
E2E Tests for Milvus SDK Instrumentation

Tests Milvus instrumentation. Requires a running Milvus instance.
Set MILVUS_HOST or run: docker compose up -d milvus-standalone
"""

import pytest
import os
import time
import uuid

from config import config


MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = int(os.getenv("MILVUS_PORT", "19530"))
HAS_MILVUS = bool(os.getenv("MILVUS_HOST"))

skip_if_no_milvus = pytest.mark.skipif(
    not HAS_MILVUS, reason="MILVUS_HOST not set"
)


@pytest.fixture(scope="module")
def milvus_client():
    """Create an instrumented Milvus client."""
    if not HAS_MILVUS:
        pytest.skip("MILVUS_HOST not set")

    from fi_instrumentation import register
    try:
        from traceai_milvus import MilvusInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_milvus not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    MilvusInstrumentor().instrument(tracer_provider=tracer_provider)

    from pymilvus import MilvusClient

    client = MilvusClient(uri=f"http://{MILVUS_HOST}:{MILVUS_PORT}")

    yield client

    MilvusInstrumentor().uninstrument()


@skip_if_no_milvus
class TestMilvusOperations:
    """Test Milvus operations."""

    def test_create_collection_and_search(self, milvus_client):
        """Test creating a collection, inserting, and searching."""
        import random

        collection_name = f"test_{uuid.uuid4().hex[:8]}"

        milvus_client.create_collection(
            collection_name=collection_name,
            dimension=128,
        )

        data = [
            {"id": i, "vector": [random.uniform(-1, 1) for _ in range(128)], "text": f"doc {i}"}
            for i in range(10)
        ]

        milvus_client.insert(collection_name=collection_name, data=data)

        results = milvus_client.search(
            collection_name=collection_name,
            data=[data[0]["vector"]],
            limit=3,
        )

        assert len(results[0]) == 3
        time.sleep(2)
        print(f"Search results: {[r['id'] for r in results[0]]}")

        # Cleanup
        milvus_client.drop_collection(collection_name=collection_name)

    def test_delete(self, milvus_client):
        """Test deleting entities."""
        import random

        collection_name = f"test_{uuid.uuid4().hex[:8]}"

        milvus_client.create_collection(
            collection_name=collection_name,
            dimension=128,
        )

        data = [
            {"id": i, "vector": [random.uniform(-1, 1) for _ in range(128)]}
            for i in range(5)
        ]

        milvus_client.insert(collection_name=collection_name, data=data)

        milvus_client.delete(
            collection_name=collection_name,
            ids=[2, 3],
        )

        # Cleanup
        milvus_client.drop_collection(collection_name=collection_name)

    def test_query(self, milvus_client):
        """Test querying with filter."""
        import random

        collection_name = f"test_{uuid.uuid4().hex[:8]}"

        milvus_client.create_collection(
            collection_name=collection_name,
            dimension=128,
        )

        data = [
            {"id": i, "vector": [random.uniform(-1, 1) for _ in range(128)], "category": "A" if i < 5 else "B"}
            for i in range(10)
        ]

        milvus_client.insert(collection_name=collection_name, data=data)

        results = milvus_client.query(
            collection_name=collection_name,
            filter='category == "A"',
        )

        assert len(results) == 5

        # Cleanup
        milvus_client.drop_collection(collection_name=collection_name)
