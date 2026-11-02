# @traceai/weaviate

Trace Weaviate collection queries, writes, and aggregations as OpenTelemetry client spans in Future AGI.

## Install

```bash
npm install @traceai/weaviate @traceai/fi-core weaviate-client
```

Use the `weaviate-client` v3 API. Current client releases require Node.js 22 or later. You also need a running Weaviate instance and a collection to query.

## Trace a collection query

Set `FI_API_KEY` and `FI_SECRET_KEY` in your application's environment using your Future AGI API credentials.

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { WeaviateInstrumentation } from "@traceai/weaviate";
import weaviate from "weaviate-client";

const provider = register({
  projectName: "weaviate-search",
  projectType: ProjectType.OBSERVE,
});

const instrumentation = new WeaviateInstrumentation({
  traceConfig: { hideErrorDetails: true },
});
instrumentation.setTracerProvider(provider);
// Instrument the mutable default export before creating a client.
instrumentation.manuallyInstrument(weaviate);

const client = await weaviate.connectToLocal();
try {
  const articles = client.collections.get("Articles");
  const results = await articles.query.fetchObjects({ limit: 5 });
  console.log(results.objects.length);
} finally {
  await client.close();
  await provider.shutdown();
}
```

Replace `Articles` with an existing collection. Open the `weaviate-search` project in Future AGI to inspect the `weaviate fetchObjects` span, including its collection name, limit, returned-row count, and duration.

If your application already has an OpenTelemetry provider and exporter, reuse that provider instead of calling `register` again. Pass it to `instrumentation.setTracerProvider(provider)`.

## Connection and collection coverage

The existing `WeaviateInstrumentation` and `manuallyInstrument` entry points support:

- Connection factories: `client`, `connectToLocal`, `connectToCustom`, `connectToWeaviateCloud`, and the legacy `connectToWCD` / `connectToWCS` aliases.
- Collection factories: `collections.get`, `use`, `create`, `createFromSchema`, and `createFromJson`, where available in your client version.
- Collection variants: `withTenant` and `withConsistency`, including chained variants.

For native ESM, instrument the **default export** as shown above. ESM named exports are read-only and cannot be manually replaced. CommonJS supports automatic instrumentation when you enable it before requiring `weaviate-client`, or manual instrumentation of the module namespace/default export. Avoid saving factory references before enabling instrumentation.

Clients and collections created before instrumentation are not retroactively instrumented. `disable()` restores patched methods on existing objects and stops tracing. After re-enabling manual instrumentation, call `manuallyInstrument` again and create a new client.

## Traced operations

| API | Methods |
| --- | --- |
| `collection.query` | `nearVector`, `nearText`, `hybrid`, `bm25`, `fetchObjects`, `fetchObjectById`, `nearObject` |
| `collection.data` | `insert`, `insertMany`, `deleteById`, `deleteMany`, `update`, `replace` |
| `collection.aggregate` and `collection.aggregate.groupBy` | `overAll`, `nearVector`, `nearText`, `nearObject`, `hybrid` (where supported by the client) |

Each supported call creates one span. Connection setup, schema management, generative queries, streaming ingestion, and reference-management methods do not get their own spans.

## Content and privacy

Query text, document properties, and vectors are omitted by default. Error messages and stack traces follow the shared trace configuration; set `hideErrorDetails: true` to omit them, since server errors can contain request data.

These opt-in settings are available under `instrumentationConfig`:

| Setting | Captures |
| --- | --- |
| `captureDocuments` | Text search inputs, inserted/updated document properties, and query-result properties |
| `captureQueryVectors` | `nearVector` input and explicit `hybrid` vectors |
| `captureResultVectors` | Vectors returned in query results |

Captured content uses `input.value` and `output.value`. `traceConfig.hideInputs` and `traceConfig.hideOutputs` override capture settings. The corresponding environment variables are `FI_HIDE_INPUTS` and `FI_HIDE_OUTPUTS`. Authentication headers, connection options, and filter expressions are not captured.

## Span attributes

| Attribute | Value |
| --- | --- |
| `fi.span.kind` | `VECTOR_DB` |
| `db.system` | `weaviate` |
| `db.operation.name` | Method name, with `aggregate.` or `aggregate.groupBy.` prefixes for aggregations |
| `db.collection.name` | Collection name |
| `db.vector.query.top_k`, `offset`, `certainty`, `distance`, `alpha` | Supplied numeric query options, under the `db.vector.query.` prefix |
| `db.vector.query.dimensions` | Length of an array passed to `nearVector` |
| `db.operation.batch_size` | Number of input objects for writes |
| `db.response.returned_rows` | Number of returned query objects |
| `db.response.inserted_count` | Successful entries in an `insertMany` result |
| `db.response.total_count` | Total count returned by an aggregation |

Rejected operations preserve the original error and mark the span as an error. Partial `insertMany` failures also mark the span as an error without exporting per-object error payloads.

## License

Apache-2.0
