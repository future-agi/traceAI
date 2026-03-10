# traceAI-qdrant

OpenTelemetry instrumentation for [Qdrant](https://qdrant.tech/) vector database.

## Installation

```bash
pip install traceAI-qdrant
```

## Quick Start

```python
from fi_instrumentation import register
from traceai_qdrant import QdrantInstrumentor
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct

trace_provider = register(project_name="my-rag-app")
QdrantInstrumentor().instrument(tracer_provider=trace_provider)

client = QdrantClient(":memory:")
client.create_collection("documents", vectors_config=VectorParams(size=384, distance=Distance.COSINE))

client.upsert("documents", points=[
    PointStruct(id=1, vector=[0.1]*384, payload={"text": "Hello"}),
    PointStruct(id=2, vector=[0.2]*384, payload={"text": "World"}),
])

results = client.search("documents", query_vector=[0.15]*384, limit=5)
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `search` | `qdrant search` | Vector similarity search |
| `upsert` | `qdrant upsert` | Insert or update points |
| `delete` | `qdrant delete` | Delete points |
| `retrieve` | `qdrant retrieve` | Get points by ID |
| `scroll` | `qdrant scroll` | Iterate through points |
| `recommend` | `qdrant recommend` | Recommendation search |
| `count` | `qdrant count` | Count points |

## License

Apache License 2.0
