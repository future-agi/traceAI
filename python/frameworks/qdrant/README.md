# traceAI-qdrant

Trace synchronous and asynchronous [Qdrant](https://qdrant.tech/) queries and point operations in Future AGI with OpenTelemetry.

## Install

```bash
pip install traceAI-qdrant "qdrant-client>=1.10.1"
```

The examples use `query_points`. The instrumentor also supports `qdrant-client>=1.6.0`, including the legacy `search` APIs when present. Async tracing requires an SDK version that provides `AsyncQdrantClient`.

## Configure tracing

Set your Future AGI API credentials in the environment:

```bash
export FI_API_KEY="<your-api-key>"
export FI_SECRET_KEY="<your-secret-key>"
```

Configure tracing once, before running Qdrant operations. The same instrumentor handles both client classes.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_qdrant import QdrantInstrumentor

trace_provider = register(
    project_name="qdrant-search",
    project_type=ProjectType.OBSERVE,
)
QdrantInstrumentor().instrument(tracer_provider=trace_provider)
```

Use one Qdrant instrumentor per process. Do not also enable another library's Qdrant instrumentation, such as OpenLLMetry, or you may create duplicate spans.

## Run a query

This example uses Qdrant's in-memory database and small sample vectors; it does not require a Qdrant server or embedding model.

```python
from qdrant_client import QdrantClient, models

client = QdrantClient(":memory:")
try:
    client.create_collection(
        "documents",
        vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
    )
    client.upsert("documents", points=[
        models.PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload={"text": "Hello"}),
        models.PointStruct(id=2, vector=[0.0, 1.0, 0.0], payload={"text": "World"}),
    ])
    results = client.query_points("documents", query=[1.0, 0.0, 0.0], limit=1)
    print(results.points[0].payload)
finally:
    client.close()
    trace_provider.force_flush()
```

Open the `qdrant-search` project in Future AGI to inspect the `qdrant upsert` and `qdrant query` spans, including collection names, result counts, scores and operation duration.

## Use an async client

Keep the same tracing setup. Await the client operations normally; spans remain open until each operation finishes.

```python
import asyncio
from qdrant_client import AsyncQdrantClient, models

async def main():
    client = AsyncQdrantClient(":memory:")
    try:
        await client.create_collection(
            "documents",
            vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        await client.upsert("documents", points=[
            models.PointStruct(id=1, vector=[1.0, 0.0, 0.0]),
        ])
        results = await client.query_batch_points("documents", requests=[
            models.QueryRequest(query=[1.0, 0.0, 0.0], limit=1),
        ])
        print(results[0].points[0].id)
    finally:
        await client.close()

asyncio.run(main())
trace_provider.force_flush()
```

## Instrumented operations

Only methods available in the installed Qdrant SDK are instrumented, for both sync and async clients.

| Methods | Span name |
|---------|-----------|
| `query_points`, `query_batch_points`, `query_points_groups` | `qdrant query` |
| Legacy `query`, `query_batch`, `search`, `search_batch`, `search_groups` | `qdrant query` |
| Legacy `recommend`, `recommend_batch`, `recommend_groups` | `qdrant recommend` |
| `upsert` | `qdrant upsert` |
| `delete` | `qdrant delete` |
| `retrieve` | `qdrant retrieve` |
| `scroll` | `qdrant scroll` |
| `count` | `qdrant count` |

Each batch or grouped call creates one client span. Batch spans record the number of requests and total returned hits; grouped spans record the number of groups and total hits. Query scores are limited to the first 10 hits. Text-query delegation does not create an extra span for the SDK's internal search call.

Collection administration, uploads, payload updates and search-matrix operations are not instrumented by this package.

## Captured data

Spans record operation metadata, not query text, embedding vectors, point payloads, point IDs or filter values. A filter is recorded only as `db.vector.query.has_filter`; the previous raw `db.vector.query.filter` attribute is no longer emitted. Payload and vector flags are recorded only when supplied as booleans, not as field selectors.

Collection names and SDK exception messages are still included. Avoid putting sensitive data in collection names, and apply your OpenTelemetry pipeline's error-redaction policy when needed. These database spans do not include retrieved document text for retrieval-quality evaluation.

## License

Apache License 2.0
