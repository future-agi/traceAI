# traceAI-lancedb

OpenTelemetry instrumentation for [LanceDB](https://lancedb.com/) embedded vector database.

## Installation

```bash
pip install traceAI-lancedb
```

## Quick Start

```python
from fi_instrumentation import register
from traceai_lancedb import LanceDBInstrumentor
import lancedb

trace_provider = register(project_name="my-rag-app")
LanceDBInstrumentor().instrument(tracer_provider=trace_provider)

db = lancedb.connect("~/.lancedb")
table = db.open_table("documents")

# All operations are traced
results = table.search([0.1, 0.2, ...]).limit(5).to_list()
table.add([{"vector": [...], "text": "..."}])
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `search` | `lancedb search` | Vector similarity search |
| `add` | `lancedb add` | Add records to table |
| `update` | `lancedb update` | Update records |
| `delete` | `lancedb delete` | Delete records |
| `create_table` | `lancedb create_table` | Create new table |
| `drop_table` | `lancedb drop_table` | Drop table |
| `open_table` | `lancedb open_table` | Open existing table |

## License

Apache License 2.0
