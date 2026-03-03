"""
E2E Tests for Weaviate SDK Instrumentation

Tests Weaviate instrumentation. Requires a running Weaviate instance.
Set WEAVIATE_URL or run: docker run -p 8080:8080 semitechnologies/weaviate
"""

import pytest
import os
import time
import uuid

from config import config


WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8080")
HAS_WEAVIATE = bool(os.getenv("WEAVIATE_URL"))

skip_if_no_weaviate = pytest.mark.skipif(
    not HAS_WEAVIATE, reason="WEAVIATE_URL not set"
)


@pytest.fixture(scope="module")
def weaviate_client():
    """Create an instrumented Weaviate client."""
    if not HAS_WEAVIATE:
        pytest.skip("WEAVIATE_URL not set")

    from fi_instrumentation import register
    try:
        from traceai_weaviate import WeaviateInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_weaviate not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    WeaviateInstrumentor().instrument(tracer_provider=tracer_provider)

    import weaviate

    client = weaviate.connect_to_local()

    yield client

    client.close()
    WeaviateInstrumentor().uninstrument()


@skip_if_no_weaviate
class TestWeaviateOperations:
    """Test Weaviate operations."""

    def test_create_collection_and_insert(self, weaviate_client):
        """Test creating a collection and inserting objects."""
        import random

        collection_name = f"Test_{uuid.uuid4().hex[:8]}"

        collection = weaviate_client.collections.create(
            name=collection_name,
            vectorizer_config=None,  # No vectorizer, provide vectors manually
        )

        vector = [random.uniform(-1, 1) for _ in range(128)]
        obj_id = collection.data.insert(
            properties={"text": "hello world"},
            vector=vector,
        )

        assert obj_id is not None
        time.sleep(2)
        print(f"Inserted object: {obj_id}")

        # Cleanup
        weaviate_client.collections.delete(collection_name)

    def test_query(self, weaviate_client):
        """Test querying objects."""
        import random

        collection_name = f"Test_{uuid.uuid4().hex[:8]}"

        collection = weaviate_client.collections.create(
            name=collection_name,
            vectorizer_config=None,
        )

        # Insert multiple objects
        for i in range(5):
            collection.data.insert(
                properties={"text": f"document {i}", "category": "A" if i < 3 else "B"},
                vector=[random.uniform(-1, 1) for _ in range(128)],
            )

        query_vector = [random.uniform(-1, 1) for _ in range(128)]
        results = collection.query.near_vector(
            near_vector=query_vector,
            limit=3,
        )

        assert len(results.objects) == 3
        print(f"Query returned {len(results.objects)} results")

        # Cleanup
        weaviate_client.collections.delete(collection_name)

    def test_delete_object(self, weaviate_client):
        """Test deleting an object."""
        import random

        collection_name = f"Test_{uuid.uuid4().hex[:8]}"

        collection = weaviate_client.collections.create(
            name=collection_name,
            vectorizer_config=None,
        )

        obj_id = collection.data.insert(
            properties={"text": "to be deleted"},
            vector=[random.uniform(-1, 1) for _ in range(128)],
        )

        collection.data.delete_by_id(obj_id)

        # Verify deleted
        result = collection.query.fetch_object_by_id(obj_id)
        assert result is None

        # Cleanup
        weaviate_client.collections.delete(collection_name)
