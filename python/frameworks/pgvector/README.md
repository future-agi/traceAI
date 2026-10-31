# traceAI-pgvector

Trace PostgreSQL queries and add pgvector distance-search metadata to the database spans in Future AGI. The package uses the maintained OpenTelemetry psycopg2 and psycopg instrumentors; it does not create a second span around their spans.

## Install

The default installation includes psycopg2 and the pgvector Python type adapters:

```bash
pip install traceAI-pgvector
```

For psycopg 3, including asynchronous connections:

```bash
pip install 'traceAI-pgvector[psycopg]'
```

Your PostgreSQL database must have the pgvector extension installed. Set `DATABASE_URL` to your database connection string and configure `FI_API_KEY` and `FI_SECRET_KEY` for Future AGI. Keep these values in your environment, not in source code.

## Trace a vector search

Enable instrumentation **before opening connections or starting a connection pool**. Use `PgVectorInstrumentor` instead of separately enabling `Psycopg2Instrumentor` or `PsycopgInstrumentor`, including through OpenTelemetry auto-instrumentation. If a driver is already instrumented, disable its instrumentation first.

```python
import os

import psycopg2
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pgvector import PgVectorInstrumentor

provider = register(project_name="pgvector-search", project_type=ProjectType.OBSERVE)
instrumentor = PgVectorInstrumentor()
instrumentor.instrument(tracer_provider=provider)

connection = psycopg2.connect(os.environ["DATABASE_URL"])
try:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(
                "CREATE TEMP TABLE documents (id integer, embedding vector(3))"
            )
            # Explicit casts work without registering Python type adapters.
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
    provider.force_flush()
    instrumentor.uninstrument()
```

Open the **pgvector-search** project in Future AGI to inspect the database spans. Queries inside an existing application span appear as its children.

If you already use `pgvector.psycopg2.register_vector()` or `pgvector.psycopg.register_vector()`, keep that setup: type registration controls data adaptation, not tracing.

## Psycopg 3 and async queries

Use the same instrumentation setup above before connecting. Both `psycopg.connect()` and `psycopg.Connection.connect()` are supported. For async applications, use `psycopg.AsyncConnection.connect()`:

```python
import asyncio
import os

import psycopg
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pgvector import PgVectorInstrumentor

provider = register(
    project_name="pgvector-async-search", project_type=ProjectType.OBSERVE
)
PgVectorInstrumentor().instrument(tracer_provider=provider)


async def search():
    async with await psycopg.AsyncConnection.connect(
        os.environ["DATABASE_URL"]
    ) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                query="SELECT '[0.1,0.2,0.3]'::vector <=> %s::vector",
                params=("[0.4,0.5,0.6]",),
            )
            print(await cursor.fetchone())


try:
    asyncio.run(search())
finally:
    provider.force_flush()
    PgVectorInstrumentor().uninstrument()
```

This example expects the `vector` extension to be enabled in your database.

## Captured data

- Each `execute` or `executemany` call produces one database CLIENT span, including ordinary INSERT and UPDATE queries. Parameters passed as iterators are not consumed by instrumentation.
- Recognized distance operators add `db.vector.operator` and `db.vector.distance_type`: `<->` (L2), `<=>` (cosine), `<#>` (inner product), `<+>` (L1), `<~>` (Hamming), and `<%>` (Jaccard).
- Recognized `ORDER BY` searches add `db.vector.query.type`. A literal numeric `LIMIT` adds `db.vector.query.top_k`; parameterized limits are not inferred from parameter values.
- Query text, parameter values, embeddings, table names, database connection details, and exception messages/stack traces are not captured. Failures set ERROR status and still raise the original exception to your application.

Vector metadata is best-effort structural detection, not schema inspection. An INSERT without a distance operator has a normal PostgreSQL span, not a fabricated vector count. Quoted literals and comments do not count as vector operations.

## Connection lifecycle and scope

Create new connections after enabling instrumentation; existing connections are not retroactively patched. Custom cursor factories passed to `connect(cursor_factory=...)` are preserved. A per-cursor factory override, server-side cursor factory, COPY operation, or a different driver such as asyncpg is outside this package's tracing scope.

`uninstrument()` restores the connection entry points and disables spans from cursors created by that installation, including cursors on connections that remain open. Re-enabling only traces new connections; reopen an existing pool to trace it again.

## License

Apache License 2.0
