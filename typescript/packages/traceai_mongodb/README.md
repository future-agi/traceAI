# @traceai/mongodb

Trace MongoDB commands and Atlas Vector Search metadata in Future AGI without changing how you use the MongoDB driver.

## Install

```bash
npm install @traceai/mongodb @traceai/fi-core mongodb
```

Use MongoDB driver 5.x, 6.x or 7.x and a Node.js version supported by that driver and the OpenTelemetry SDK. Vector search requires a deployment with Atlas Vector Search and a vector index matching your embeddings.

## Configure tracing

Set `FI_API_KEY` and `FI_SECRET_KEY` to your Future AGI project credentials, and `MONGODB_URI` to your database connection string. Keep these values out of source control.

Initialize instrumentation before connecting. Explicit module instrumentation also works when your application imports MongoDB before the tracing setup, including native ESM imports.

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { MongoDBInstrumentation } from "@traceai/mongodb";
import * as mongodb from "mongodb";

const provider = register({
  projectName: "mongodb-app",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new MongoDBInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(mongodb);

const client = new mongodb.MongoClient(process.env.MONGODB_URI!, {
  monitorCommands: true,
});

async function main() {
  try {
    await client.connect();
    const documents = client.db("app").collection("documents");
    const results = await documents.find({ category: "guides" }).limit(5).toArray();
    console.log(`Found ${results.length} documents`);
  } finally {
    await client.close();
    instrumentation.disable();
    await provider.shutdown();
  }
}

main().catch(console.error);
```

Open the `mongodb-app` project in Future AGI and inspect the `mongodb find` span. Further cursor batches appear as `mongodb getMore` spans.

If you already have an OpenTelemetry provider, pass that provider to `setTracerProvider()` instead of calling `register()` again. Use only one MongoDB instrumentor: disable `@opentelemetry/instrumentation-mongodb` in an auto-instrumentation bundle when using this package.

## Trace vector search

Use the driver's normal cursor API. The query vector must match the dimensions of your configured index.

```typescript
const results = await client.db("app").collection("documents").aggregate([
  {
    $vectorSearch: {
      index: "vector_index",
      path: "embedding",
      queryVector: [0.1, 0.2, 0.3],
      numCandidates: 100,
      limit: 5,
    },
  },
]).toArray();
```

`$vectorSearch` and legacy `$search.knnBeta` commands include the index name, vector dimensions, candidate count and result limit when present. The package never records vector values, query/filter contents or returned documents.

## Span behavior

- Spans describe executed database commands, not calls that construct a cursor. Creating or closing an unused cursor produces no span.
- `find()`, `aggregate()`, chaining, `findOne()`, async iteration and early cursor close retain their normal behavior. Instrumentation does not call `toArray()` or drain a cursor.
- Insert, update, delete, search-index and read commands are traced. Authentication and handshakes are excluded. Batched writes and cursor iteration can produce multiple command spans.
- `disable()` removes command listeners and restores instrumented methods. In-flight spans end without a success status; database work continues normally. `enable()` resumes tracing.
- For a client that is already connected, construct it with `monitorCommands: true` and call `instrumentation.instrumentClient(client)`. Otherwise, initialize instrumentation before connecting.
- Command monitoring does not report failures that happen before a command is sent, such as invalid arguments or server selection failures. These errors still reach your application unchanged.

| Attribute | Meaning |
| --- | --- |
| `fi.span.kind` | `VECTOR_DB` |
| `db.system`, `db.system.name` | `mongodb` |
| `db.namespace` | Database name |
| `db.collection.name` | Collection name, when provided by the command |
| `db.operation.name` | Wire command name, such as `find`, `aggregate` or `insert` |
| `db.operation.type` | `vector_search` for supported vector stages |
| `db.vector.query.top_k` | Requested vector result limit |
| `db.vector.query.dimensions` | Query vector length |
| `db.vector.query.num_candidates` | Requested candidate count |
| `db.vector.index_name` | Vector search index name |
| `db.response.returned_rows` | Documents in this response batch, not the full cursor |
| `db.operation.batch_size` | Documents in an insert command |
| `db.response.inserted_count`, `db.response.modified_count`, `db.response.deleted_count` | Write result counts, when present |

## Update operation-based dashboards

Spans now follow database commands rather than Collection method calls. For example, both `insertOne()` and `insertMany()` produce `mongodb insert` spans with `db.operation.name=insert`; update and delete methods similarly use `update` and `delete`. Adjust filters that used method names. Treat `getMore` spans and `db.response.returned_rows` as individual response batches rather than a materialized full result set.

## Privacy and configuration

The existing constructor options remain supported:

```typescript
const instrumentation = new MongoDBInstrumentation({
  instrumentationConfig: { enabled: true },
  traceConfig: { hideErrorDetails: true },
});
```

Error messages and stack traces are hidden by default because database errors can contain query values. Set `traceConfig.hideErrorDetails` to `false` only if you intend to export those details. Error status and exception type remain available when details are hidden.

`captureQueryVectors` and `captureResultVectors` are deprecated compatibility options. They do not enable payload capture. Database, collection and index names are metadata and remain visible.

## License

Apache-2.0
