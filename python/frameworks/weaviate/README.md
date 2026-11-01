# traceAI-weaviate

Trace Weaviate collection searches and writes in Future AGI with the maintained Python client instrumentor.

## Install

```bash
pip install traceAI-weaviate
```

Use the Weaviate v4 Python client. Synchronous collections are supported from the first stable v4 release (4.4.0); asynchronous collections require 4.7.0 or later.

## Trace a search

Set your Future AGI credentials in your application environment:

```bash
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
```

This example uses a local Weaviate instance with an existing `Documents` collection containing searchable text properties. Enable instrumentation once, before making collection requests:

```python
import weaviate
from fi_instrumentation import TraceConfig, register
from traceai_weaviate import WeaviateInstrumentor

provider = register(project_name="my-rag-app")
WeaviateInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_error_details=True),
)

with weaviate.connect_to_local() as client:
    documents = client.collections.get("Documents")
    results = documents.query.bm25(query="machine learning", limit=5)
    print(f"Found {len(results.objects)} documents")

provider.force_flush()
```

Open **my-rag-app** in Future AGI and inspect the `weaviate bm25` span. It includes the collection, operation, requested limit, returned-object count and status. Searches have the `RETRIEVER` semantic kind; data operations have `VECTOR_DB`. Each operation is an OpenTelemetry `CLIENT` span under the active parent span.

The same instrumentor supports async collections. After the setup above, use the SDK's async API normally:

```python
import asyncio

async def search():
    async with weaviate.use_async_with_local() as client:
        documents = client.collections.get("Documents")
        results = await documents.query.bm25(query="machine learning", limit=5)
        print(f"Found {len(results.objects)} documents")

asyncio.run(search())
provider.force_flush()
```

## Supported operations

| Collection method | Span name |
| --- | --- |
| `query.near_vector` | `weaviate near_vector` |
| `query.near_text` | `weaviate near_text` |
| `query.hybrid` | `weaviate hybrid` |
| `query.bm25` | `weaviate bm25` |
| `query.fetch_objects` | `weaviate fetch_objects` |
| `data.insert` | `weaviate insert` |
| `data.insert_many` | `weaviate insert_many` |
| `data.delete_by_id` | `weaviate delete_by_id` |
| `data.delete_many` | `weaviate delete_many` |

Both sync and async versions of these methods are traced. Administration, aggregation, generation, other query methods, updates, references and background batch context managers are not instrumented. `db.operation.name` identifies the method, such as `near_text` or `insert_many`. Limits and similarity thresholds are recorded only when supplied; the instrumentor does not guess server defaults. Partial batch failures set an error status and a failed-object count without capturing their error bodies.

## Control captured content

By default, spans contain operation metadata, not query text, vectors, filters, inserted properties or returned document properties. Exception messages can contain application data: use `TraceConfig(hide_error_details=True)`, as above, or set `FI_HIDE_ERROR_DETAILS=true` to remove exception messages, stack traces and error status descriptions while retaining the error type.

To opt in to text queries and returned document properties, set `capture_content=True` on the initial `instrument()` call:

```python
WeaviateInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_error_details=True),
    capture_content=True,
)
```

This captures `near_text`, `hybrid` and `bm25` query text and the properties of at most 20 returned objects from ordinary, non-grouped queries. Each text attribute is limited to 16,384 characters. Vectors, filters, references, inserted properties and batch error bodies are never captured. `TraceConfig` input/output hide flags and their `FI_HIDE_*` environment equivalents override content capture; PII redaction also applies. Collection names remain metadata.

To change configuration, call `uninstrument()` before enabling instrumentation again. Calling `instrument()` repeatedly does not add another layer of spans.

## Pause or disable tracing

```python
from fi_instrumentation.instrumentation import suppress_tracing

with suppress_tracing():
    # Supported Weaviate calls made here do not produce instrumentor spans.
    pass

WeaviateInstrumentor().uninstrument()
```

Uninstrumenting restores the SDK methods. It does not close your Weaviate client or shut down the tracer provider; your application retains ownership of both.

## License

Apache License 2.0
