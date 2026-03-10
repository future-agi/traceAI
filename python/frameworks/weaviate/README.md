# traceAI-weaviate

OpenTelemetry instrumentation for [Weaviate](https://weaviate.io/) vector database (v4 API).

## Installation

```bash
pip install traceAI-weaviate
```

## Quick Start

```python
from fi_instrumentation import register
from traceai_weaviate import WeaviateInstrumentor
import weaviate

trace_provider = register(project_name="my-rag-app")
WeaviateInstrumentor().instrument(tracer_provider=trace_provider)

client = weaviate.connect_to_local()
collection = client.collections.get("Documents")

# All operations are traced
results = collection.query.near_text(query="machine learning", limit=5)
collection.data.insert(properties={"title": "My Doc", "content": "..."})
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `near_vector` | `weaviate near_vector` | Vector similarity search |
| `near_text` | `weaviate near_text` | Text-based semantic search |
| `hybrid` | `weaviate hybrid` | Hybrid BM25 + vector search |
| `bm25` | `weaviate bm25` | Keyword search |
| `fetch_objects` | `weaviate fetch_objects` | Fetch objects |
| `insert` | `weaviate insert` | Insert single object |
| `insert_many` | `weaviate insert_many` | Batch insert |
| `delete_by_id` | `weaviate delete_by_id` | Delete by ID |
| `delete_many` | `weaviate delete_many` | Batch delete |

## License

Apache License 2.0
