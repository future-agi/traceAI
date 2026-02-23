# traceAI-pgvector

OpenTelemetry instrumentation for [pgvector](https://github.com/pgvector/pgvector) PostgreSQL extension.

## Installation

```bash
pip install traceAI-pgvector
```

## Quick Start

```python
from fi_instrumentation import register
from traceai_pgvector import PgVectorInstrumentor
import psycopg2
from pgvector.psycopg2 import register_vector

trace_provider = register(project_name="my-rag-app")
PgVectorInstrumentor().instrument(tracer_provider=trace_provider)

conn = psycopg2.connect("postgresql://...")
register_vector(conn)

# All vector operations are traced
cur = conn.cursor()
cur.execute(
    "SELECT * FROM documents ORDER BY embedding <-> %s LIMIT 5",
    ([0.1, 0.2, ...],)
)
```

## Instrumented Operations

The instrumentor captures PostgreSQL queries that contain pgvector operations:
- `<->` - L2 distance
- `<#>` - Inner product distance
- `<=>` - Cosine distance
- `<+>` - L1 distance

| Query Type | Detection | Captured Attributes |
|------------|-----------|---------------------|
| Vector similarity search | `ORDER BY ... <->` | distance type, limit |
| Insert with vector | `INSERT INTO ... vector` | table name, vector count |
| Update with vector | `UPDATE ... SET vector` | table name |

## License

Apache License 2.0
