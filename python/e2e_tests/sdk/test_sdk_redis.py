"""
E2E Tests for Redis Vector SDK Instrumentation

Tests Redis vector search instrumentation. Requires Redis with RediSearch module.
Set REDIS_URL or run: docker run -p 6379:6379 redis/redis-stack
"""

import pytest
import os
import time
import uuid

from config import config


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
HAS_REDIS = bool(os.getenv("REDIS_URL"))

skip_if_no_redis = pytest.mark.skipif(
    not HAS_REDIS, reason="REDIS_URL not set"
)


@pytest.fixture(scope="module")
def redis_client():
    """Create an instrumented Redis client."""
    if not HAS_REDIS:
        pytest.skip("REDIS_URL not set")

    from fi_instrumentation import register
    try:
        from traceai_redis import RedisInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_redis not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    RedisInstrumentor().instrument(tracer_provider=tracer_provider)

    import redis

    client = redis.from_url(REDIS_URL)

    yield client

    RedisInstrumentor().uninstrument()


@skip_if_no_redis
class TestRedisVectorOperations:
    """Test Redis vector search operations."""

    def test_create_index_and_search(self, redis_client):
        """Test creating an index and vector search."""
        import numpy as np
        from redis.commands.search.field import VectorField, TextField
        from redis.commands.search.indexDefinition import IndexDefinition, IndexType
        from redis.commands.search.query import Query

        index_name = f"idx_{uuid.uuid4().hex[:8]}"
        prefix = f"doc:{index_name}:"

        # Create index
        try:
            redis_client.ft(index_name).create_index(
                [
                    TextField("content"),
                    VectorField(
                        "embedding",
                        "FLAT",
                        {"TYPE": "FLOAT32", "DIM": 128, "DISTANCE_METRIC": "COSINE"},
                    ),
                ],
                definition=IndexDefinition(prefix=[prefix], index_type=IndexType.HASH),
            )
        except Exception:
            pass  # Index may already exist

        # Insert vectors
        import random

        for i in range(5):
            vec = np.array([random.uniform(-1, 1) for _ in range(128)], dtype=np.float32)
            redis_client.hset(
                f"{prefix}{i}",
                mapping={
                    "content": f"document {i}",
                    "embedding": vec.tobytes(),
                },
            )

        time.sleep(1)

        # Search
        query_vec = np.array([random.uniform(-1, 1) for _ in range(128)], dtype=np.float32)
        q = (
            Query("*=>[KNN 3 @embedding $vec AS score]")
            .sort_by("score")
            .return_fields("content", "score")
            .dialect(2)
        )

        results = redis_client.ft(index_name).search(
            q, query_params={"vec": query_vec.tobytes()}
        )

        assert results.total > 0
        print(f"Search results: {results.total} hits")

        # Cleanup
        redis_client.ft(index_name).dropindex(delete_documents=True)
