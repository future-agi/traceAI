# traceAI-milvus

OpenTelemetry instrumentation for [Milvus](https://milvus.io/) vector database.

## Installation

```bash
pip install traceAI-milvus
```

## Quick Start

```python
from fi_instrumentation import register
from traceai_milvus import MilvusInstrumentor
from pymilvus import MilvusClient

trace_provider = register(project_name="my-rag-app")
MilvusInstrumentor().instrument(tracer_provider=trace_provider)

client = MilvusClient("milvus.db")

# All operations are traced
client.search(
    collection_name="documents",
    data=[[0.1, 0.2, ...]],
    limit=5
)
client.insert(collection_name="documents", data=[...])
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `search` | `milvus search` | Vector similarity search |
| `query` | `milvus query` | Scalar query with filters |
| `insert` | `milvus insert` | Insert vectors |
| `upsert` | `milvus upsert` | Upsert vectors |
| `delete` | `milvus delete` | Delete vectors |
| `get` | `milvus get` | Get vectors by ID |
| `create_collection` | `milvus create_collection` | Create collection |
| `drop_collection` | `milvus drop_collection` | Drop collection |

## License

Apache License 2.0
