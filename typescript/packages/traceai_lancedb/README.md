# @traceai/lancedb

Trace LanceDB database operations and query execution with OpenTelemetry in Node.js.

## Install

```bash
npm install @traceai/lancedb @traceai/fi-core @lancedb/lancedb@0.41.0 \
  @opentelemetry/api @opentelemetry/context-async-hooks
```

Use Node.js 20 or later. This integration supports `@lancedb/lancedb` versions `>=0.4.20 <=0.41.0`; the example uses 0.41.0. Older SDK versions expose vector search as `table.vectorSearch(...)` rather than `table.search(...)`.

## Send traces to Future AGI

Set your Future AGI credentials before running the application:

```bash
export FI_API_KEY="YOUR_FI_API_KEY"
export FI_SECRET_KEY="YOUR_FI_SECRET_KEY"
```

Save the following as `lancedb-tracing.mjs`. Use the **default import** for manual instrumentation, and instrument it before calling `connect`. An ESM namespace's named exports are immutable snapshots, so calling a previously imported named `connect` bypasses the manual hook.

```javascript
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { register, ProjectType } from "@traceai/fi-core";
import { LanceDBInstrumentation } from "@traceai/lancedb";
import lancedb from "@lancedb/lancedb";

// Omit this line if your existing OpenTelemetry SDK already installs a context manager.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const provider = register({
  projectName: "lancedb-search",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new LanceDBInstrumentation({
  traceConfig: { hideErrorDetails: true },
});
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(lancedb);

const db = await lancedb.connect("./lancedb-example");
try {
  const table = await db.createTable("documents", [
    { id: 1, text: "Search documentation", vector: [1, 0] },
    { id: 2, text: "Deployment guide", vector: [0, 1] },
  ], { mode: "overwrite" });
  const results = await table.search([1, 0]).limit(1).toArray();
  console.log(results.map(row => row.id));
} finally {
  db.close();
  instrumentation.disable();
  await provider.shutdown();
  context.disable();
}
```

```bash
node lancedb-tracing.mjs
```

The example overwrites only the `documents` table in `./lancedb-example`. In Future AGI, open **Observe**, select `lancedb-search`, and inspect the `lancedb createTable` and `lancedb search` spans.

If you already run an OpenTelemetry SDK, reuse its provider and context manager instead of registering a second provider. With CommonJS, register `new LanceDBInstrumentation()` with your SDK **before** requiring `@lancedb/lancedb` for automatic instrumentation; manual instrumentation remains available for a module you loaded earlier.

## Operations and query lifecycle

Connections created through the instrumented `connect` trace `createTable`, `openTable`, `dropTable`, and `tableNames`. Their tables trace `add`, `update`, `delete`, and `countRows`.

`search`, `vectorSearch`, and `query` still return synchronous fluent builders. Building a query creates no span. Executing it with `toArray`, `toArrow`, `execute`, or `for await` creates one `lancedb search` span, including nested calls made by the SDK. Reusing a builder creates a new span for each execution.

Iterator spans end on exhaustion, early `for await` exit, explicit `return()`, or an error. If you manually consume an iterator, exhaust it or call `return()` when finished. Abandoning it without closing it leaves its span open.

The integration does not trace every LanceDB API. Index management, catalog/namespace connections, merge-insert builders, and branch operations are outside this coverage. LanceDB's `instrumentLanceDbMetrics` exports metrics, not these database traces.

## Attributes and privacy

| Attribute | Meaning |
| --- | --- |
| `db.system` | `lancedb` |
| `db.operation.name` | Database operation, or `search` for query execution |
| `fi.span.kind` | `VECTOR_DB` |
| `db.collection.name` | Table name, when available |
| `db.vector.query.dimensions` | Length of an explicitly supplied vector array |
| `db.vector.query.top_k` | Explicit query limit |
| `db.operation.batch_size` | Number of array records passed to `add` |
| `db.response.returned_rows` | Array/Arrow result size or number of streamed rows |

Query vectors, record contents, filter expressions, connection URIs, and credentials are not captured. Table names and numeric metadata are exported. Errors normally include messages and stack traces; set `traceConfig.hideErrorDetails: true` to retain error status without those details.

`captureQueryVectors` and `captureResultVectors` remain accepted for source compatibility, but are deprecated and do not enable content capture.

## Enable and disable

`instrumentation.disable()` restores the original module and prototype methods, including hooks installed manually. `instrumentation.enable()` restores owned hooks for existing connections and builders. Operations already in flight finish their existing spans.

## Troubleshooting

- **No database spans:** instrument before connecting, supply a configured tracer provider, and use the mutable default import for ESM manual instrumentation.
- **Duplicate query spans:** ensure your OpenTelemetry SDK has an active context manager, as shown above. Do not layer a second database instrumentor over the same calls.
- **An iterator span never finishes:** exhaust the iterator or call `return()`; use `toArray()` for small results you want to collect completely.
- **A Cloud connection fails on an old SDK:** upgrade LanceDB. The 0.4.20 native package does not include Cloud transport support; the integration does not add it.

## License

Apache-2.0
