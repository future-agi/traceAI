"""
E2E Tests for MongoDB Vector SDK Instrumentation

Tests MongoDB vector search instrumentation. Requires MongoDB Atlas or local MongoDB 7+.
Set MONGODB_URI to enable.
"""

import pytest
import os
import time
import uuid

from config import config


MONGODB_URI = os.getenv("MONGODB_URI")
HAS_MONGODB = bool(MONGODB_URI)

skip_if_no_mongodb = pytest.mark.skipif(
    not HAS_MONGODB, reason="MONGODB_URI not set"
)


@pytest.fixture(scope="module")
def mongodb_collection():
    """Create an instrumented MongoDB collection."""
    if not HAS_MONGODB:
        pytest.skip("MONGODB_URI not set")

    from fi_instrumentation import register
    try:
        from traceai_mongodb import MongoDBInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_mongodb not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    MongoDBInstrumentor().instrument(tracer_provider=tracer_provider)

    from pymongo import MongoClient

    client = MongoClient(MONGODB_URI)
    db = client["e2e_test_db"]
    collection_name = f"test_{uuid.uuid4().hex[:8]}"
    collection = db[collection_name]

    yield collection

    # Cleanup
    collection.drop()
    client.close()

    MongoDBInstrumentor().uninstrument()


@skip_if_no_mongodb
class TestMongoDBOperations:
    """Test MongoDB operations."""

    def test_insert_and_find(self, mongodb_collection):
        """Test inserting documents and finding."""
        import random

        docs = [
            {
                "text": f"document {i}",
                "embedding": [random.uniform(-1, 1) for _ in range(128)],
                "category": "A" if i < 5 else "B",
            }
            for i in range(10)
        ]

        mongodb_collection.insert_many(docs)

        result = mongodb_collection.find_one({"text": "document 0"})
        assert result is not None
        assert result["text"] == "document 0"

        time.sleep(2)
        print(f"Found document: {result['text']}")

    def test_update(self, mongodb_collection):
        """Test updating documents."""
        mongodb_collection.insert_one({"text": "original", "version": 1})

        mongodb_collection.update_one(
            {"text": "original"},
            {"$set": {"version": 2}},
        )

        result = mongodb_collection.find_one({"text": "original"})
        assert result["version"] == 2

    def test_delete(self, mongodb_collection):
        """Test deleting documents."""
        mongodb_collection.insert_many([
            {"text": "keep", "type": "good"},
            {"text": "remove", "type": "bad"},
        ])

        mongodb_collection.delete_one({"type": "bad"})

        result = mongodb_collection.find_one({"type": "bad"})
        assert result is None

    def test_aggregate(self, mongodb_collection):
        """Test aggregation pipeline."""
        mongodb_collection.insert_many([
            {"text": "a", "score": 10},
            {"text": "b", "score": 20},
            {"text": "c", "score": 30},
        ])

        pipeline = [
            {"$group": {"_id": None, "avg_score": {"$avg": "$score"}}},
        ]

        results = list(mongodb_collection.aggregate(pipeline))
        assert len(results) > 0
        print(f"Avg score: {results[0]['avg_score']}")
