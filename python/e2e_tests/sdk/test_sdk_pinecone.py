"""
E2E Tests for Pinecone SDK Instrumentation

Tests Pinecone instrumentation. Requires PINECONE_API_KEY.
"""

import pytest
import time
import uuid

from config import config, skip_if_no_pinecone


@pytest.fixture(scope="module")
def pinecone_index():
    """Create an instrumented Pinecone index."""
    if not config.has_pinecone():
        pytest.skip("PINECONE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_pinecone import PineconeInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_pinecone not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    PineconeInstrumentor().instrument(tracer_provider=tracer_provider)

    from pinecone import Pinecone

    pc = Pinecone(api_key=config.pinecone_api_key)

    # Use existing index or create serverless
    index_name = "e2e-test-index"
    existing = [idx.name for idx in pc.list_indexes()]

    if index_name not in existing:
        from pinecone import ServerlessSpec

        pc.create_index(
            name=index_name,
            dimension=128,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        # Wait for index to be ready
        import time as t
        t.sleep(10)

    index = pc.Index(index_name)

    yield index

    PineconeInstrumentor().uninstrument()


@skip_if_no_pinecone
class TestPineconeOperations:
    """Test Pinecone operations."""

    def test_upsert_and_query(self, pinecone_index):
        """Test upserting vectors and querying."""
        import random

        namespace = f"test_{uuid.uuid4().hex[:8]}"

        vectors = [
            {
                "id": f"vec_{i}",
                "values": [random.uniform(-1, 1) for _ in range(128)],
                "metadata": {"text": f"document {i}"},
            }
            for i in range(5)
        ]

        pinecone_index.upsert(vectors=vectors, namespace=namespace)
        time.sleep(2)  # Wait for indexing

        results = pinecone_index.query(
            vector=vectors[0]["values"],
            top_k=3,
            namespace=namespace,
            include_metadata=True,
        )

        assert len(results["matches"]) > 0
        print(f"Query results: {[m['id'] for m in results['matches']]}")

        # Cleanup
        pinecone_index.delete(ids=[v["id"] for v in vectors], namespace=namespace)

    def test_fetch(self, pinecone_index):
        """Test fetching vectors by ID."""
        import random

        namespace = f"test_{uuid.uuid4().hex[:8]}"

        vectors = [
            {
                "id": "fetch_test_1",
                "values": [random.uniform(-1, 1) for _ in range(128)],
                "metadata": {"text": "fetch test"},
            }
        ]

        pinecone_index.upsert(vectors=vectors, namespace=namespace)
        time.sleep(2)

        result = pinecone_index.fetch(ids=["fetch_test_1"], namespace=namespace)

        assert "fetch_test_1" in result["vectors"]

        # Cleanup
        pinecone_index.delete(ids=["fetch_test_1"], namespace=namespace)

    def test_delete(self, pinecone_index):
        """Test deleting vectors."""
        import random

        namespace = f"test_{uuid.uuid4().hex[:8]}"

        vectors = [
            {
                "id": f"del_{i}",
                "values": [random.uniform(-1, 1) for _ in range(128)],
            }
            for i in range(3)
        ]

        pinecone_index.upsert(vectors=vectors, namespace=namespace)
        time.sleep(2)

        pinecone_index.delete(ids=["del_1"], namespace=namespace)
        time.sleep(1)

        result = pinecone_index.fetch(ids=["del_1"], namespace=namespace)
        assert "del_1" not in result.get("vectors", {})

        # Cleanup remaining
        pinecone_index.delete(ids=["del_0", "del_2"], namespace=namespace)
