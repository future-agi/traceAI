# @traceai/milvus

Trace Milvus gRPC operations with OpenTelemetry, including operation duration, collection names, result counts, and errors.

## Install

```bash
npm install @traceai/milvus @zilliz/milvus2-sdk-node
```

Configure your application's OpenTelemetry tracer provider and trace exporter before making database calls. See [Future AGI tracing](https://docs.futureagi.com/docs/tracing) for exporter setup.

## Instrument a client

Instrument the module **before constructing clients**. Milvus saves some methods, including `hybridSearch`, on each instance during construction.

```typescript
import { MilvusInstrumentation } from "@traceai/milvus";
import * as milvus from "@zilliz/milvus2-sdk-node";

const instrumentation = new MilvusInstrumentation();
instrumentation.manuallyInstrument(milvus);

const client = new milvus.MilvusClient({ address: "localhost:19530" });

// Use an existing collection and a vector that matches its dimension.
async function searchArticles(queryVector: number[]) {
  return client.search({
    collection_name: "articles",
    data: queryVector,
    limit: 10,
  });
}
```

The example uses the direct-client API in modern 2.x and 3.x SDKs. You can also register `MilvusInstrumentation` with your OpenTelemetry SDK's `instrumentations` option before loading the Milvus SDK; the manual module call is useful when the module has already been imported.

### Existing clients and saved aliases

If a client was constructed before instrumentation was enabled, instrument that instance as well. Patching the module cannot discover previously created clients or replace aliases they already saved.

```typescript
instrumentation.manuallyInstrumentClient(client);
```

For the SDK 2.0 manager API, pass the relevant manager instead:

```typescript
instrumentation.manuallyInstrumentClient(legacyClient.dataManager);
instrumentation.manuallyInstrumentClient(legacyClient.collectionManager);
instrumentation.manuallyInstrumentClient(legacyClient.indexManager);
```

`legacyClient` is your existing SDK 2.0 client. Only the supported method names listed below are wrapped; manager methods with other names are not traced by this package.

### Enable and disable

```typescript
instrumentation.disable(); // Restore methods and stop tracing saved wrappers.
instrumentation.enable();  // Resume tracing previously registered targets.
```

Repeated setup does not add duplicate wrappers. If multiple instrumentation instances share a target, it stays traced until all owners disable it. The first active owner supplies the tracer and configuration; normally, use one instrumentation instance per application. Calls already in progress finish their spans even if you disable instrumentation while they run.

## Supported operations

When present on the target, the package wraps:

- Search: `search`, `hybridSearch`, `query`, `get`
- Data: `insert`, `upsert`, `delete`
- Collections: `createCollection`, `dropCollection`, `hasCollection`, `describeCollection`
- Indexes: `createIndex`, `dropIndex`

In SDKs where `hybridSearch` is an alias of `search`, both produce a `milvus search` span. SDK-internal calls to other supported operations can produce child operation spans.

The separate Milvus HTTP client is not instrumented by the module entry point. Use the gRPC `MilvusClient` for this integration. `loadCollection` and `releaseCollection` are also outside this package's operation list.

## Span attributes

Each operation creates a `CLIENT` span under the active application span.

| Attribute | Value |
| --- | --- |
| `db.system` | `milvus` |
| `db.operation.name` | Wrapped operation name |
| `fi.span.kind` | `VECTOR_DB` |
| `db.collection.name` | `collection_name`, when supplied |
| `db.vector.query.top_k` | Top-level `limit` or `topk` for search/query |
| `db.vector.query.dimensions` | First vector's length when `vectors` is supplied |
| `db.operation.batch_size` | Length of `data` or `fields_data` for insert/upsert |
| `db.response.returned_rows` | Length of a returned `results` array |

The package does not record query vectors, result vectors, filters, or result payloads. The existing `captureQueryVectors` and `captureResultVectors` configuration fields are retained for API compatibility but do not enable vector capture. Collection names and exception details can appear in spans.

## Milvus native tracing

Milvus SDK 3.0.6's `trace: true` option adds a `grpc-intercept` span and propagates trace context into gRPC metadata. That span ends before the RPC continues, so it does not replace this package's operation-duration and collection attributes.

Leave native tracing off unless you need propagation into the Milvus service. If you enable it, use the same OpenTelemetry provider: a native propagation span can appear beneath the operation span. Configure only one application trace exporter for your destination to avoid exporting the same spans twice.

## Troubleshooting

- **Search is traced but an existing client's hybrid search is not:** call `manuallyInstrumentClient(client)` or construct the client after module instrumentation.
- **No spans appear:** configure an OpenTelemetry provider/exporter before making calls, check that instrumentation is enabled, and confirm you are using the gRPC rather than HTTP client.
- **Tracing continues after disabling one instance:** disable the other instrumentation instances that registered the same target. Already-started operations still complete normally.

## License

Apache-2.0
