# traceAI-chromadb

Trace ChromaDB collection reads, writes, and retrievals with OpenTelemetry and view them in Future AGI.

## Install

```bash
pip install traceAI-chromadb
```

## Initialize tracing

Set your Future AGI credentials before starting the application:

```bash
export FI_API_KEY="YOUR_FI_API_KEY"
export FI_SECRET_KEY="YOUR_FI_SECRET_KEY"
```

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_chromadb import ChromaDBInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="chroma-retrieval",
)
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)
```

Initialize once per process, before calling collection methods. Use only one
collection-level Chroma instrumentor; do not combine this package with another
Chroma collection instrumentor. Chroma's native HTTP transport tracing covers a
different layer and may produce additional child spans.

## Run a retrieval

This example supplies small illustrative vectors directly, so no embedding model
or model API key is needed. In your application, supply your own embeddings or
configure a Chroma embedding function.

```python
import chromadb

client = chromadb.EphemeralClient()
collection = client.create_collection("sample-documents", embedding_function=None)
collection.add(
    ids=["guide", "faq"],
    embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    documents=["Getting started guide", "Frequently asked questions"],
)
results = collection.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)
print(results["ids"])
trace_provider.force_flush()
```

Open **Observe** in Future AGI and select `chroma-retrieval`. The example produces
`chroma add` and `chroma query` spans. Existing application spans remain their
parents when you use the same tracer provider.

### Async HTTP client

On Chroma versions that provide `AsyncHttpClient`, the same initialization traces
awaited collection operations. Connect to your running Chroma server:

```python
import asyncio
import chromadb

async def main():
    client = await chromadb.AsyncHttpClient(host="localhost", port=8000)
    collection = await client.get_or_create_collection(
        "sample-documents", embedding_function=None
    )
    await collection.upsert(ids=["guide"], embeddings=[[1.0, 0.0, 0.0]])
    results = await collection.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)
    print(results["ids"])

asyncio.run(main())
trace_provider.force_flush()
```

## Supported operations

These methods are traced on both `Collection` and `AsyncCollection`, when the
installed Chroma version exposes them. Async spans cover the awaited operation,
not just coroutine creation. Positional and keyword arguments are supported;
NumPy inputs are supported where Chroma accepts them.

| Method | Span name | Captured details |
|---|---|---|
| `add` | `chroma add` | Item count and supplied embedding dimensions |
| `upsert` | `chroma upsert` | Item count and supplied embedding dimensions |
| `update` | `chroma update` | Item count and which fields were supplied |
| `query` | `chroma query` | Requested result count, filters, first-query result summary |
| `search` | `chroma search` | Search count and first-search result summary |
| `get` | `chroma get` | ID count, filters, pagination and returned item count |
| `delete` | `chroma delete` | Requested ID count and filters |
| `count` | `chroma count` | Collection item count |
| `peek` | `chroma peek` | Requested limit and returned item count |

`search` requires a Chroma version and deployment supporting its Search API.
Client/collection lifecycle methods (such as create, modify, fork, or delete
collection), indexing status and attached functions are not instrumented by this
package. Embedding model calls require their own provider instrumentation.

## Span attributes and data capture

Every operation includes `db.system="chroma"`, `db.operation.name`,
`db.namespace` and `db.vector.collection.name`. Spans use `SpanKind.CLIENT`.
Failures retain the original exception and are recorded as span errors.

| Attribute | Meaning |
|---|---|
| `db.vector.query.top_k` | `query`'s actual `n_results` argument |
| `db.vector.query.filter` | JSON `where` filter |
| `db.vector.query.where_document` | JSON document filter |
| `db.vector.query.include` | Requested result fields |
| `db.vector.query.type` | `embedding` or `text`, for those query input types |
| `db.vector.search.count` | Number of Search requests |
| `db.vector.results.count` | Returned items; for `query`/`search`, first request only |
| `db.vector.results.ids` | Up to ten IDs from the first query/search result |
| `db.vector.results.scores` | Up to ten distances (`query`) or scores (`search`) |
| `db.vector.upsert.count` | Number of items supplied to `add`/`upsert` |
| `db.vector.upsert.dimensions` | Dimensions of explicitly supplied embeddings |

The instrumentor does not attach document bodies, query text or embedding vectors
to successful spans. Collection names, filters, result IDs and exception messages
can still contain sensitive information. Review those fields before exporting
sensitive workloads; this package does not implement its own content-redaction
configuration. Search rank/filter expressions are not serialized.

To stop tracing collection operations, call:

```python
ChromaDBInstrumentor().uninstrument()
```

## Resources

- [ChromaDB documentation](https://docs.trychroma.com/)
- [Future AGI documentation](https://docs.futureagi.com/)
- [OpenTelemetry database conventions](https://opentelemetry.io/docs/specs/semconv/database/)

## License

Apache License 2.0
