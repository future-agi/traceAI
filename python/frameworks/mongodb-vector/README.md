# traceAI-mongodb

OpenTelemetry instrumentation for [MongoDB Atlas Vector Search](https://www.mongodb.com/docs/atlas/atlas-vector-search/).

## Installation

```bash
pip install traceAI-mongodb
```

## Quick Start

```python
from fi_instrumentation import register
from traceai_mongodb import MongoDBInstrumentor
from pymongo import MongoClient

trace_provider = register(project_name="my-rag-app")
MongoDBInstrumentor().instrument(tracer_provider=trace_provider)

client = MongoClient("mongodb+srv://...")
db = client["mydb"]
collection = db["documents"]

# Vector search operations are traced
results = collection.aggregate([
    {
        "$vectorSearch": {
            "index": "vector_index",
            "path": "embedding",
            "queryVector": [0.1, 0.2, ...],
            "numCandidates": 100,
            "limit": 5
        }
    }
])
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `aggregate` | `mongodb aggregate` | Aggregation pipeline (includes $vectorSearch) |
| `insert_one` | `mongodb insert_one` | Insert single document |
| `insert_many` | `mongodb insert_many` | Batch insert |
| `update_one` | `mongodb update_one` | Update single document |
| `update_many` | `mongodb update_many` | Batch update |
| `delete_one` | `mongodb delete_one` | Delete single document |
| `delete_many` | `mongodb delete_many` | Batch delete |
| `find` | `mongodb find` | Query documents |
| `find_one` | `mongodb find_one` | Find single document |

## Vector Search Detection

The instrumentor automatically detects `$vectorSearch` stages in aggregation pipelines and captures:
- Vector index name
- Query vector dimensions
- numCandidates
- limit

## License

Apache License 2.0
