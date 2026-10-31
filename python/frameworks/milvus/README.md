# traceAI-milvus

Trace synchronous and asynchronous [Milvus](https://milvus.io/) client operations in Future AGI, including hybrid searches and iterator page fetches.

## Install

```bash
pip install traceAI-milvus
```

You need a running Milvus server and PyMilvus 2.3.0 or later. Async, hybrid-search, upsert and iterator support depend on the APIs available in your installed PyMilvus version. This package instruments `MilvusClient` and `AsyncMilvusClient`, not the legacy ORM `Collection` API.

## Configure tracing

Set your Future AGI credentials and Milvus connection details in your environment:

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
export MILVUS_URI="http://localhost:19530"
# Set MILVUS_TOKEN if your Milvus server requires authentication.
```

Configure instrumentation once, before calling the client:

```python
import os

from fi_instrumentation import TraceConfig, register
from pymilvus import MilvusClient
from traceai_milvus import MilvusInstrumentor

trace_provider = register(project_name="my-rag-app")
instrumentor = MilvusInstrumentor()
instrumentor.instrument(
    tracer_provider=trace_provider,
    config=TraceConfig(hide_inputs=True, hide_error_details=True),
)

client = MilvusClient(
    uri=os.environ["MILVUS_URI"],
    token=os.environ.get("MILVUS_TOKEN", ""),
)
client.create_collection(collection_name="tracing_example", dimension=2)
client.insert(
    collection_name="tracing_example",
    data=[
        {"id": 1, "vector": [0.1, 0.2]},
        {"id": 2, "vector": [0.3, 0.4]},
    ],
)
results = client.search(
    collection_name="tracing_example",
    data=[[0.1, 0.2]],
    limit=2,
)
print(results)
client.close()
trace_provider.force_flush()
```

In Future AGI, open **my-rag-app** and inspect the `milvus create_collection`, `milvus insert` and `milvus search` spans. Spans include the collection, operation, duration, status and available result counts.

Use an existing tracer provider instead of calling `register()` again if your application already configures tracing. Enable only one direct PyMilvus instrumentor to avoid duplicate database spans.

## Async operations

The same `MilvusInstrumentor` setup also instruments `AsyncMilvusClient` when your PyMilvus version provides it. Create the async client inside your application's event loop:

```python
from pymilvus import AsyncMilvusClient

async def search_async():
    client = AsyncMilvusClient(
        uri=os.environ["MILVUS_URI"],
        token=os.environ.get("MILVUS_TOKEN", ""),
    )
    try:
        return await client.search(
            collection_name="tracing_example",
            data=[[0.1, 0.2]],
            limit=2,
        )
    finally:
        await client.close()
```

Async spans cover the awaited operation, including failures and cancellation, rather than just coroutine creation.

## Iterators

For synchronous `query_iterator()` and `search_iterator()`, iterator creation, each `.next()` call and `.close()` each produce a separate span. No pages are fetched by instrumentation, and spans do not remain open while your application processes a page. Fetches use the active parent span at the time of the call.

```python
iterator = client.query_iterator(
    collection_name="tracing_example",
    batch_size=100,
    filter="id >= 0",
)
try:
    while True:
        page = iterator.next()
        if not page:
            break
        print(page)
finally:
    iterator.close()
```

Use an open `MilvusClient` for this example. Always close the iterator to release its SDK resources, even when you stop before the last page.

## Privacy

The package never records vector values, inserted documents, raw IDs, hybrid request bodies or result content. It records counts and operational metadata such as collection names, limits and dimensions.

Filters and requested output-field names are recorded by default. Set `TraceConfig(hide_inputs=True)` or `TraceConfig(hide_input_text=True)` to omit them, or set `FI_HIDE_INPUTS=true` before enabling instrumentation. Collection names remain visible; do not put sensitive content in collection names.

Set `hide_error_details=True` or `FI_HIDE_ERROR_DETAILS=true` to omit exception messages, stack traces and error status descriptions. The span still records an error status and exception type. Error-detail hiding is also enabled automatically when you set `hide_inputs=True`, unless explicitly overridden.

## Supported operations

| Client operation | Span name |
| --- | --- |
| `search` | `milvus search` |
| `hybrid_search` | `milvus hybrid_search` |
| `query` | `milvus query` |
| `insert` | `milvus insert` |
| `upsert` | `milvus upsert` |
| `delete` | `milvus delete` |
| `get` | `milvus get` |
| `create_collection` | `milvus create_collection` |
| `drop_collection` | `milvus drop_collection` |
| `search_iterator` | `milvus search_iterator`, `.next`, `.close` |
| `query_iterator` | `milvus query_iterator`, `.next`, `.close` |

Only methods present in the installed SDK are instrumented. PyMilvus does not expose async iterator methods in the supported client surface.

## Disable tracing

```python
instrumentor.uninstrument()
```

This restores the methods installed by this instrumentor and disables tracing on iterator handles it already returned. Calling `instrument()` again does not stack wrappers. Disabling instrumentation does not close clients, iterators or your tracer provider.

## License

Apache License 2.0
