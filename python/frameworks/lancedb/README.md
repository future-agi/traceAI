# traceAI-lancedb

Trace LanceDB query execution and table operations with OpenTelemetry. The same instrumentor supports local and remote tables and LanceDB's synchronous and asynchronous APIs.

## Install

```bash
pip install traceAI-lancedb
```

## Trace a local search

Set `FI_API_KEY` and `FI_SECRET_KEY` in your application environment. For self-hosted Future AGI, set `FI_BASE_URL` to your collector origin.

```python
import lancedb
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_lancedb import LanceDBInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="lancedb-search")
instrumentor = LanceDBInstrumentor()
instrumentor.instrument(tracer_provider=provider)

try:
    db = lancedb.connect("./lancedb-demo")
    table = db.create_table(
        "documents",
        data=[
            {"id": 1, "vector": [1.0, 0.0], "text": "violet flower"},
            {"id": 2, "vector": [0.0, 1.0], "text": "blue sea"},
        ],
        mode="overwrite",
    )
    results = table.search([1.0, 0.0]).limit(2).to_list()
    print(results)
finally:
    provider.force_flush()
```

Open **Observe** in Future AGI and select `lancedb-search`. The example produces a `lancedb create_table` span and one `lancedb search` span. Calling `to_list()` does not create extra spans for the `to_arrow()` and `to_batches()` calls it makes internally.

To use your application's existing OpenTelemetry setup instead, pass its tracer provider to `instrument(tracer_provider=provider)`. Register one provider per process.

## Supported operations

| API | Span |
| --- | --- |
| Vector, full-text, hybrid and scan query materialization | `lancedb search` |
| `add`, `update`, `delete` on local, remote or async tables | `lancedb add`, `lancedb update`, `lancedb delete` |
| `create_table`, `open_table`, `drop_table` on local, remote or async connections | `lancedb create_table`, `lancedb open_table`, `lancedb drop_table` |

Query materialization includes `to_list`, `to_arrow`, `to_batches`, `to_pandas`, `to_df`, `to_pydantic` and `to_polars` where available in your LanceDB version. Building a query with `search()`, `query()`, `where()` or `limit()` does not itself execute a query or create a span. Index creation, maintenance and SQL execution APIs are not instrumented.

### Streaming batches

The span stays open while you consume a batch reader. Consume it to exhaustion or close it when you stop early:

```python
with table.search([1.0, 0.0]).limit(2).to_batches() as batches:
    for batch in batches:
        print(batch.num_rows)
```

The reader remains lazy and supports Arrow's C stream interface. Completed readers report the number of returned rows. Early close or garbage collection ends the span with the count consumed so far and an unset status, rather than claiming successful completion. Errors and async cancellation end the span with error status. The span is current only during database work, not while your application processes each batch.

Async queries use the same setup:

```python
import asyncio

async def search_async():
    db = await lancedb.connect_async("./lancedb-demo")
    table = await db.open_table("documents")
    reader = await table.vector_search([1.0, 0.0]).limit(2).to_batches()
    try:
        async for batch in reader:
            print(batch.num_rows)
    finally:
        await reader.aclose()

asyncio.run(search_async())
provider.force_flush()
```

## Recorded data

Spans include the operation, table name when available, output format, result count and query limit when exposed by the SDK. They do not capture vectors, query text, SQL filters, row contents, connection credentials or exception messages/stack traces. Error status and exception type are retained. Table names are metadata; avoid including sensitive information in them.

These limits apply to this instrumentor's spans. Other instrumentors and your application may capture additional information.

## Disable instrumentation

```python
instrumentor.uninstrument()
```

This restores the patched methods. Readers already in progress finish their existing spans when consumed or closed. Calling `instrument()` again enables tracing without stacking duplicate patches.

## License

Apache License 2.0
