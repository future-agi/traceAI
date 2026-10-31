"""Run a small pgvector search without changing existing application tables.

Install traceAI-pgvector and set DATABASE_URL, FI_API_KEY and FI_SECRET_KEY.
PostgreSQL must have the pgvector extension installed. The example creates the
extension if needed (requires permission) and uses a connection-local temp table.
"""

import os

import psycopg2
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pgvector import PgVectorInstrumentor


def main() -> None:
    provider = register(
        project_name="pgvector-search", project_type=ProjectType.OBSERVE
    )
    instrumentor = PgVectorInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        connection = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute(
                    "CREATE TEMP TABLE documents (id integer, embedding vector(3))"
                )
                cursor.executemany(
                    "INSERT INTO documents VALUES (%s, %s::vector)",
                    iter([(1, "[0.1,0.2,0.3]"), (2, "[0.4,0.5,0.6]")]),
                )
                cursor.execute(
                    "SELECT id FROM documents ORDER BY embedding <-> %s::vector LIMIT 5",
                    ("[0.1,0.2,0.3]",),
                )
                print(cursor.fetchall())
        finally:
            connection.close()
    finally:
        provider.force_flush()
        instrumentor.uninstrument()


if __name__ == "__main__":
    main()
