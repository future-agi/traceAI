# traceAI-pinecone

OpenTelemetry instrumentation for [Pinecone](https://www.pinecone.io/) vector database.

## Installation

```bash
pip install traceAI-pinecone
```

## Quick Start

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pinecone import PineconeInstrumentor
import pinecone

# Set up environment
os.environ["FI_API_KEY"] = "<your-api-key>"
os.environ["FI_SECRET_KEY"] = "<your-secret-key>"

# Register tracer
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-rag-app"
)

# Instrument Pinecone
PineconeInstrumentor().instrument(tracer_provider=trace_provider)

# Trace the supported vector operations on your existing index.
# Set PINECONE_API_KEY and PINECONE_INDEX_HOST in your environment.
pc = pinecone.Pinecone(api_key=os.environ["PINECONE_API_KEY"])
index = pc.Index(host=os.environ["PINECONE_INDEX_HOST"])

# Query vectors
results = index.query(
    vector=[0.1] * 1536,
    top_k=10,
    include_metadata=True
)

# Upsert vectors
index.upsert(
    vectors=[
        {"id": "vec1", "values": [0.1] * 1536, "metadata": {"title": "Doc 1"}},
        {"id": "vec2", "values": [0.2] * 1536, "metadata": {"title": "Doc 2"}},
    ]
)
```

Open your `my-rag-app` project in Future AGI to find the `pinecone query` and
`pinecone upsert` spans. Use vectors with the dimension configured on your index.

## Instrumented Operations

The same six operations are instrumented on synchronous REST `Index`, asynchronous
REST `AsyncIndex` (or legacy `IndexAsyncio`), and synchronous gRPC `GrpcIndex`
(or legacy `GRPCIndex`). Your installed Pinecone SDK determines which classes are
available; older SDKs may require their `asyncio` or `grpc` extra. Synchronous REST
retains the `pinecone>=3.0.0` dependency floor. For legacy async or gRPC clients,
use `pinecone[asyncio,grpc]>=7.3.0,<8`, or use SDK 10 with the client names below.
The SDK 3 gRPC extra pins a protobuf version incompatible with this package's
OpenTelemetry dependency; upgrade Pinecone before enabling gRPC.

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `query` | `pinecone query` | Vector similarity search |
| `upsert` | `pinecone upsert` | Insert or update vectors |
| `delete` | `pinecone delete` | Delete vectors by ID or filter |
| `fetch` | `pinecone fetch` | Retrieve vectors by ID |
| `update` | `pinecone update` | Update vector metadata |
| `describe_index_stats` | `pinecone describe_index_stats` | Get index statistics |

Async spans stay open until the awaited operation completes, fails, or is
cancelled. `PineconeInstrumentor().uninstrument()` restores the original methods
on all instrumented classes. Enable only one Pinecone instrumentor in your process
to avoid duplicate operation spans.

Coverage is limited to the operations above. Document, record/search, inference,
assistant, index-management, namespace-management, and import APIs are not
instrumented. Multi-namespace helpers have no aggregate span; any supported query
they invoke may produce its own span. Legacy `async_req=True` and
`async_threadpool_executor=True` calls pass through without a span. Dedicated gRPC
`*_async` future APIs have no future-lifecycle coverage; use awaited REST methods
or synchronous gRPC methods when you need end-to-end operation spans.

### Async REST and gRPC

For Pinecone SDK 10, keep the tracer setup above and use either of these clients.
Both use the existing `PineconeInstrumentor` entry point; no separate async
instrumentor is needed.

```python
import asyncio
from pinecone import AsyncIndex

async def query_async():
    async with AsyncIndex(
        api_key=os.environ["PINECONE_API_KEY"],
        host=os.environ["PINECONE_INDEX_HOST"],
    ) as index:
        return await index.query(vector=[0.1] * 1536, top_k=5)

results = asyncio.run(query_async())
```

```python
from pinecone.grpc import GrpcIndex

with GrpcIndex(
    api_key=os.environ["PINECONE_API_KEY"],
    host=os.environ["PINECONE_INDEX_HOST"],
) as index:
    results = index.query(vector=[0.1] * 1536, top_k=5)
```

## Span Attributes

The instrumentor records operation metadata, counts, filters, up to ten query
result IDs and scores, update IDs, and exception details. It does not record
vector values or document metadata. Treat filters, IDs, and errors as potentially
sensitive when configuring access to your telemetry. Index names are included
only when exposed by the SDK; the instrumentor does not infer names from hosts.

### Common Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.system` | string | Always `"pinecone"` |
| `db.operation.name` | string | Operation name (query, upsert, etc.) |
| `db.namespace` | string | Index name |
| `db.vector.namespace` | string | Pinecone namespace |
| `db.vector.index.name` | string | Index name |
| `db.vector.index.host` | string | Pinecone host URL |

### Query Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.vector.query.top_k` | int | Number of results requested |
| `db.vector.query.filter` | string | JSON filter expression |
| `db.vector.query.include_metadata` | bool | Whether metadata is included |
| `db.vector.query.include_vectors` | bool | Whether vectors are included |
| `db.vector.results.count` | int | Number of results returned |
| `db.vector.results.scores` | string | JSON array of top scores |
| `db.vector.results.ids` | string | JSON array of result IDs |

### Upsert Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.vector.upsert.count` | int | Number of vectors upserted |
| `db.vector.upsert.dimensions` | int | Vector dimensions |

### Delete Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.vector.delete.count` | int | Number of vectors deleted |
| `db.vector.delete.all` | bool | Whether all vectors deleted |

## RAG Pipeline Example

```python
from fi_instrumentation import register
from traceai_pinecone import PineconeInstrumentor
from traceai_openai import OpenAIInstrumentor
import pinecone
import openai

# Register tracer
trace_provider = register(project_name="rag-pipeline")

# Instrument both Pinecone and OpenAI
PineconeInstrumentor().instrument(tracer_provider=trace_provider)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# Initialize clients
pc = pinecone.Pinecone()
index = pc.Index("documents")
client = openai.OpenAI()

def rag_query(question: str) -> str:
    # 1. Generate embedding for question
    embedding_response = client.embeddings.create(
        model="text-embedding-3-small",
        input=question
    )
    query_embedding = embedding_response.data[0].embedding

    # 2. Search Pinecone for relevant documents
    results = index.query(
        vector=query_embedding,
        top_k=5,
        include_metadata=True
    )

    # 3. Build context from results
    context = "\n".join([
        match.metadata.get("text", "")
        for match in results.matches
    ])

    # 4. Generate answer with context
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": f"Context:\n{context}"},
            {"role": "user", "content": question}
        ]
    )

    return response.choices[0].message.content

# All steps are traced with correlated spans!
answer = rag_query("What is retrieval augmented generation?")
```

## Testing

### Unit Tests

```bash
cd python/frameworks/pinecone
pip install pytest pytest-asyncio
pytest tests/ -v
```

### Example

Run the example RAG pipeline:

```bash
cd examples
python basic_rag.py
```

## Resources

- [Pinecone Documentation](https://docs.pinecone.io/)
- [TraceAI Documentation](https://docs.futureagi.com/)
- [OpenTelemetry Database Conventions](https://opentelemetry.io/docs/specs/semconv/database/)
- [Vector DB Guide](../../docs/VECTOR_DB_GUIDE.md)

## License

Apache License 2.0
