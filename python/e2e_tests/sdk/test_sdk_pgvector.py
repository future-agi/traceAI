"""
E2E Tests for pgvector SDK Instrumentation

Tests pgvector instrumentation. Requires PostgreSQL with pgvector extension.
"""

import pytest
import os
import time

from config import config


PG_VECTOR_HOST = os.getenv("PG_VECTOR_HOST", config.pg_host)
PG_VECTOR_PORT = os.getenv("PG_VECTOR_PORT", config.pg_port)
PG_VECTOR_DB = os.getenv("PG_VECTOR_DB", "pgvector_test")
PG_VECTOR_USER = os.getenv("PG_VECTOR_USER", config.pg_user)
PG_VECTOR_PASSWORD = os.getenv("PG_VECTOR_PASSWORD", config.pg_password)
HAS_PGVECTOR = bool(os.getenv("PG_VECTOR_HOST"))

skip_if_no_pgvector = pytest.mark.skipif(
    not HAS_PGVECTOR, reason="PG_VECTOR_HOST not set"
)


@pytest.fixture(scope="module")
def pgvector_conn():
    """Create an instrumented pgvector connection."""
    if not HAS_PGVECTOR:
        pytest.skip("PG_VECTOR_HOST not set")

    from fi_instrumentation import register
    try:
        from traceai_pgvector import PgVectorInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_pgvector not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    PgVectorInstrumentor().instrument(tracer_provider=tracer_provider)

    import psycopg2
    from pgvector.psycopg2 import register_vector

    conn = psycopg2.connect(
        host=PG_VECTOR_HOST,
        port=PG_VECTOR_PORT,
        dbname=PG_VECTOR_DB,
        user=PG_VECTOR_USER,
        password=PG_VECTOR_PASSWORD,
    )
    conn.autocommit = True

    register_vector(conn)

    # Create extension and test table
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("DROP TABLE IF EXISTS e2e_test_items")
    cur.execute("""
        CREATE TABLE e2e_test_items (
            id serial PRIMARY KEY,
            content text,
            embedding vector(128)
        )
    """)
    cur.close()

    yield conn

    # Cleanup
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS e2e_test_items")
    cur.close()
    conn.close()

    PgVectorInstrumentor().uninstrument()


@skip_if_no_pgvector
class TestPgVectorOperations:
    """Test pgvector operations."""

    def test_insert_and_query(self, pgvector_conn):
        """Test inserting vectors and nearest neighbor search."""
        import random
        import numpy as np

        cur = pgvector_conn.cursor()

        # Insert vectors
        for i in range(10):
            vec = np.array([random.uniform(-1, 1) for _ in range(128)])
            cur.execute(
                "INSERT INTO e2e_test_items (content, embedding) VALUES (%s, %s)",
                (f"document {i}", vec),
            )

        # Query nearest neighbors
        query_vec = np.array([random.uniform(-1, 1) for _ in range(128)])
        cur.execute(
            "SELECT id, content, embedding <-> %s AS distance FROM e2e_test_items ORDER BY embedding <-> %s LIMIT 3",
            (query_vec, query_vec),
        )

        results = cur.fetchall()
        assert len(results) == 3

        time.sleep(2)
        print(f"Nearest neighbors: {[(r[0], r[2]) for r in results]}")
        cur.close()
