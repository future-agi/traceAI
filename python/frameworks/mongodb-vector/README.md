# traceAI-mongodb

Trace MongoDB commands and Atlas Vector Search metadata in Future AGI using the [maintained OpenTelemetry PyMongo instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/pymongo/pymongo.html).

## Install

```bash
pip install traceAI-mongodb
```

PyMongo 4.6 or later (below 5.0) is required. Use a PyMongo version with `AsyncMongoClient` for async applications; Motor is not covered by this package.

## Configure tracing

Set `FI_API_KEY`, `FI_SECRET_KEY` and `MONGODB_URI` in your application's environment. Obtain your Future AGI keys from your workspace settings and your MongoDB connection string from your database administrator or Atlas.

Instrument **before creating clients**. PyMongo attaches command listeners when a client is constructed; recreate clients that predate instrumentation.

```python
import os

from fi_instrumentation import register
from pymongo import MongoClient
from traceai_mongodb import MongoDBInstrumentor

trace_provider = register(project_name="my-rag-app")
MongoDBInstrumentor().instrument(tracer_provider=trace_provider)

with MongoClient(os.environ["MONGODB_URI"]) as client:
    collection = client["mydb"]["documents"]
    documents = list(collection.find({}).limit(5))

trace_provider.force_flush()
```

The same instrumentor works with `pymongo.AsyncMongoClient`; await database operations and iterate async cursors normally. Open the `my-rag-app` project in Future AGI to inspect the `mydb.find` command and any subsequent `mydb.getMore` commands.

Do not also enable `PymongoInstrumentor` or the `pymongo` integration through OpenTelemetry auto-instrumentation. If that instrumentor is already active, traceAI retains its configuration without adding vector enrichment or a second listener. `MongoDBInstrumentor().uninstrument()` disables only tracing enabled by this adapter.

## Trace Atlas Vector Search

For an Atlas collection with a vector search index named `vector_index` on a three-dimensional `embedding` field, replace the query above with:

```python
results = list(collection.aggregate([
    {
        "$vectorSearch": {
            "index": "vector_index",
            "path": "embedding",
            "queryVector": [0.1, 0.2, 0.3],
            "numCandidates": 100,
            "limit": 5,
        }
    }
]))
```

Use your index name, embedding path and query embedding with the same dimensions as your index. The `mydb.aggregate` span includes the index name, field, vector dimensions, candidate count and requested limit. Vector values and filters are not exported.

## What is recorded

Each span covers one actual MongoDB command, including commands issued by sync and async clients. A lazy `find()` call emits nothing until the cursor issues a command. Reading more batches creates `getMore` spans; failures belong to the command that failed. Commands are children of the active OpenTelemetry span when they are sent, not when a cursor is constructed.

- Standard MongoDB attributes describe the database, collection and server address. `db.operation.name` contains the command name.
- `db.vector.*` attributes add vector search metadata and server-reported insert, update and delete counts. Query result counts describe the returned batch, not the whole cursor.
- Filters, document bodies, query vectors and server error messages are not captured. Error spans contain a generic description and a numeric server code when available. Database, collection, index and field names remain visible.
- These are database command spans, not LLM calls: no tokens, costs, retrieved document contents or relevance scores are inferred.

**Migrating from method-based tracing:** the `MongoDBInstrumentor` entry point is unchanged, but span names now follow `<database>.<command>` (for example, `mydb.insert` instead of `mongodb insert_many`). Update dashboards that match the old names or per-method operation values. Use the upstream database/collection attributes instead of the old collection-valued `db.namespace`. Batched writes and cursor pagination can produce multiple command spans. Server selection failures before a command is sent do not produce command spans.

Calling `uninstrument()` stops new spans, closes outstanding command spans with an error status, and leaves application methods untouched. A later `instrument(tracer_provider=...)` reuses the listener, including on existing clients that already have it, with the new provider.

## License

Apache License 2.0
