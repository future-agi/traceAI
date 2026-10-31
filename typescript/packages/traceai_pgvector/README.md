# @traceai/pgvector

Trace PostgreSQL queries with pgvector distance metrics, vector dimensions, result counts, and database errors using OpenTelemetry.

## Install

Use the JavaScript `pg` driver (8.x) and a PostgreSQL database with the pgvector extension. The example uses Node.js 22 or later.

```bash
npm install @traceai/pgvector pg pgvector @opentelemetry/sdk-node@^0.202.0 @opentelemetry/exporter-trace-otlp-http@^0.202.0
```

## Configure tracing

Start your OpenTelemetry SDK before loading `pg`. Reuse an existing SDK/provider if your application already has one; do not create a second provider. For Future AGI authentication and exporter setup, see [fi-core](../fi-core/README.md).

The standard OTLP exporter reads `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS`. Point these at your collector or configured trace endpoint. For a local collector:

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4318/v1/traces
```

Save this as `search.cjs`. Set `DATABASE_URL` to your database connection string before running it.

```javascript
const { NodeSDK } = require("@opentelemetry/sdk-node");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-http");
const { PgVectorInstrumentation } = require("@traceai/pgvector");

const instrumentation = new PgVectorInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true },
});
const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter(),
  instrumentations: [instrumentation],
});
sdk.start();

const pg = require("pg");
const pgvector = require("pgvector");
const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });

async function main() {
  try {
    const result = await pool.query(
      "SELECT id FROM documents ORDER BY embedding <=> $1::vector LIMIT $2",
      [pgvector.toSql([0.1, 0.2, 0.3]), 5],
    );
    console.log(result.rows);
  } finally {
    await pool.end();
    instrumentation.disable();
    await sdk.shutdown();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
```

The example expects a `documents` table with an `embedding vector(3)` column. To create sample data:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE documents (id integer PRIMARY KEY, embedding vector(3));
INSERT INTO documents VALUES (1, '[0.1,0.2,0.3]'), (2, '[0.4,0.2,0.1]');
```

Run `node search.cjs`. In your trace viewer, find the `pgvector vectorSearch` span. It includes the cosine distance metric, three query dimensions, the requested limit, and the number of returned rows.

### If pg was already imported

After enabling the instrumentation and registering it with your SDK, call `instrumentation.manuallyInstrument(pg)` with the imported module. This also supports ESM applications whose imports load `pg` before setup. Calling it repeatedly does not add duplicate wrappers. `disable()` restores the query method, and `enable()` restores instrumentation for manually registered modules.

Use **one PostgreSQL query instrumentor**. Do not also enable `@opentelemetry/instrumentation-pg` (including through an auto-instrumentation bundle) for the same driver.

## Configuration

Pass package settings under `instrumentationConfig`, and privacy settings under `traceConfig`:

```typescript
new PgVectorInstrumentation({
  instrumentationConfig: { captureQueries: false },
  traceConfig: { hideInputs: true, hideOutputs: true },
});
```

| Setting | Behavior |
| --- | --- |
| `enabled` | Enables automatic instrumentation; defaults to `true`. |
| `captureQueries` | Includes SQL text, truncated after 1,000 characters; defaults to `false`. |
| `captureStatements` | Deprecated alias for `captureQueries`. An explicit `captureQueries` value takes precedence. |
| `captureParameters`, `captureQueryVectors`, `captureResultVectors` | Deprecated, ignored settings. Raw parameters, vectors, and row contents are never exported by this package. |

`traceConfig.hideInputs` and `hideInputText` suppress SQL even when `captureQueries` is enabled. Use `hideErrorDetails` to suppress exception text and stacks; it is also enabled by default when `hideInputs` or `hideOutputs` is true. These are the real fi-core options, not `maskInputs` or `maskOutputs`. Corresponding fi-core environment settings, including `FI_HIDE_INPUTS` and `FI_HIDE_ERROR_DETAILS`, also apply.

SQL and database error messages can contain sensitive data. Leave statement capture off unless you need it, and enable error-detail hiding for applications that process private content.

## Span behavior

- `Client.query` produces one execution span. `Pool.query` delegates to that method and does not produce a second execution span.
- Promise and callback queries end on completion, not on submission. Callback returns, callback context, synchronous errors, and result/error objects retain the driver's behavior.
- `pg.Query` and EventEmitter-style Submittables end on their callback, `end`, or `error`. Custom Submittables that are immutable or have neither a callback nor an emitter pass through without a span.
- This package does not instrument connection setup or pool acquisition. A pool acquisition failure is not a query execution.
- Ordinary SQL has database attributes but no `fi.span.kind=VECTOR_DB`. Vector detection is heuristic: it recognizes distance operators and explicit vector types, not your database schema. Inserts without a vector cast may therefore remain ordinary database spans.

| Attribute | Meaning |
| --- | --- |
| `db.system` | `postgresql` |
| `db.operation.name` | `query`, `insert`, `update`, `delete`, or `vectorSearch` |
| `fi.span.kind` | `VECTOR_DB` only when vector syntax is detected |
| `db.operation.type` | `vector_search` when a distance operator is detected |
| `db.vector.distance_metric` | `l2`, `inner_product`, `cosine`, `l1`, `hamming`, or `jaccard` |
| `db.vector.query.dimensions` | Dimensions of a dense numeric vector parameter directly following a distance operator, including `pgvector.toSql()` strings |
| `db.vector.query.top_k` | A single numeric `LIMIT`, including a bound parameter; omitted when ambiguous |
| `db.response.returned_rows` | Number of rows in a single query result |
| `db.response.affected_rows` | Numeric `rowCount`, when available |
| `db.statement` | Opt-in SQL text |

No table name is guessed from arbitrary SQL, and no metrics or logs exporter is installed.

## Troubleshooting

**No spans:** register the instrumentation with your SDK and load it before `pg`, or call `manuallyInstrument(pg)` after imports. Await SDK shutdown in short-lived scripts.

**Duplicate query spans:** disable other PostgreSQL query instrumentation in your auto-instrumentation bundle.

**No vector dimensions:** use `pgvector.toSql()` for a dense vector parameter immediately following the distance operator. SQL expressions, column-to-column comparisons, and sparse/bit vectors do not expose dense query dimensions.

## License

Apache-2.0
