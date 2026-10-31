"""Optional real-driver tests against a disposable localhost pgvector database.

Set PGVECTOR_TEST_PORT only for a database created for these tests. All data is
synthetic, all tables are temporary, and the only accepted host is localhost.
"""

import asyncio
import os

import psycopg
import psycopg2
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_pgvector import PgVectorInstrumentor

PORT = os.environ.get("PGVECTOR_TEST_PORT")
pytestmark = pytest.mark.skipif(
    not PORT, reason="Requires disposable localhost pgvector database"
)


@pytest.fixture
def database():
    dsn = f"host=127.0.0.1 port={int(PORT)} dbname=postgres user=postgres password=synthetic-local-only"
    # Extension creation is a one-time setup operation, not an application span.
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = PgVectorInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield dsn, exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("driver", [psycopg2, psycopg])
def test_real_sync_driver_and_type_registration(database, driver):
    dsn, exporter = database
    if driver is psycopg2:
        from pgvector.psycopg2 import register_vector
    else:
        from pgvector.psycopg import register_vector
    connection = driver.connect(dsn)
    try:
        register_vector(connection)
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE items (id integer, embedding vector(3))")
        exporter.clear()
        cursor.executemany(
            "INSERT INTO items VALUES (%s, %s::vector)",
            iter([(1, "[1,2,3]"), (2, "[2,3,4]")]),
        )
        kwargs = {"vars" if driver is psycopg2 else "params": ("[1,2,3]",)}
        cursor.execute(
            query="SELECT id FROM items ORDER BY embedding <-> %s::vector LIMIT 1",
            **kwargs,
        )
        assert cursor.fetchone()[0] == 1
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert spans[0].attributes["db.operation.name"] == "insert"
        assert spans[1].attributes["db.vector.distance_type"] == "l2_distance"
        assert spans[1].attributes["db.vector.query.top_k"] == 1
    finally:
        connection.close()


def test_real_async_driver_and_type_registration(database):
    dsn, exporter = database
    from pgvector.psycopg import register_vector_async

    async def run():
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await register_vector_async(connection)
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "CREATE TEMP TABLE items (id integer, embedding vector(3))"
                )
                exporter.clear()
                await cursor.executemany(
                    query="INSERT INTO items VALUES (%s, %s::vector)",
                    params_seq=iter([(1, "[1,2,3]"), (2, "[2,3,4]")]),
                )
                await cursor.execute(
                    query="SELECT id FROM items ORDER BY embedding <=> %s::vector LIMIT 1",
                    params=("[1,2,3]",),
                )
                assert (await cursor.fetchone())[0] == 1
                # Inspect before connection context exit commits the transaction.
                spans = exporter.get_finished_spans()
                assert len(spans) == 2
                assert (
                    spans[1].attributes["db.vector.distance_type"] == "cosine_distance"
                )

    asyncio.run(run())
