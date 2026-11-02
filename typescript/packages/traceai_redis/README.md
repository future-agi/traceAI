# @traceai/redis

Trace Redis hash, JSON and RediSearch operations with OpenTelemetry, including vector-search metadata and result counts.

## Install

```bash
npm install @traceai/redis @traceai/fi-core redis
```

Use Node.js 18.19 or later and `redis` 4.x, 5.x or 6.x. JSON and search commands also require the corresponding Redis server capabilities.

## Send traces to Future AGI

Set `FI_API_KEY` and `FI_SECRET_KEY` to your Future AGI credentials, and `REDIS_URL` to your Redis connection URL. Keep credentials in your environment or secret manager, not in source code.

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { RedisInstrumentation } from "@traceai/redis";
import * as redisModule from "redis";

const provider = register({
  projectName: "redis-search",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new RedisInstrumentation({
  traceConfig: { hideErrorDetails: true },
});
instrumentation.setTracerProvider(provider);

// Use the returned module: native ESM namespace bindings are read-only.
const redis = instrumentation.manuallyInstrument(redisModule);
const client = redis.createClient({ url: process.env.REDIS_URL });
client.on("error", (error) => console.error(error));
await client.connect();

try {
  await client.hSet("product:1", { name: "Trail shoes" });
  const name = await client.hGet("product:1", "name");
  console.log(name);
} finally {
  await client.quit();
  instrumentation.disable();
  await provider.shutdown();
}
```

Open the `redis-search` project in Future AGI to inspect `redis hSet` and `redis hGet` spans. Each supported command produces a client span; commands inside an active OpenTelemetry span become its children.

If your application already has an OpenTelemetry provider, reuse it with `setTracerProvider()` instead of registering a second provider.

## Instrument existing clients

For a client created before instrumentation, or one returned by `duplicate()`, instrument the client directly. Its identity and command results are preserved.

```typescript
const client = instrumentation.instrumentClient(
  redisModule.createClient({ url: process.env.REDIS_URL }),
);
```

CommonJS modules can also be patched in place with `manuallyInstrument(require("redis"))`. Automatic CommonJS instrumentation is available when you construct the instrumentor before loading `redis`. For native ESM, use the returned module as shown above rather than previously imported factory bindings. Do not combine this package with another Redis command-tracing instrumentor on the same clients.

Calling `manuallyInstrument()` or `instrumentClient()` repeatedly does not add duplicate spans. `disable()` restores this instrumentor's factories and client methods, including manually instrumented clients. `enable()` resumes tracing on still-live registered modules and clients. Clients created while disabled must be passed to `instrumentClient()` after re-enabling.

## Trace vector searches

For an existing RediSearch index named `idx:products` with a three-dimensional FLOAT32 vector field named `embedding`:

```typescript
const results = await client.ft.search(
  "idx:products",
  "*=>[KNN 10 @embedding $vector AS score]",
  {
    PARAMS: { vector: Buffer.from(new Float32Array([0.1, 0.2, 0.3]).buffer) },
    RETURN: ["name", "score"],
    SORTBY: { BY: "score" },
    LIMIT: { from: 0, size: 10 },
    DIALECT: 2,
  },
);
```

The span records the index name, vector-search operation type, requested result limit and returned counts. It does not record the query text, vector bytes or returned documents.

## Supported operations

The following direct client methods and their uppercase aliases are traced:

| Redis methods | Span operation |
| --- | --- |
| `ft.search`, `ft.create`, `ft.dropIndex`, `ft.info`, `ft.aggregate` | `ftSearch`, `ftCreate`, `ftDropIndex`, `ftInfo`, `ftAggregate` |
| `hSet`, `hGet`, `hGetAll`, `del` | `hSet`, `hGet`, `hGetAll`, `del` |
| `json.set`, `json.get` | `json.set`, `json.get` |

Factory wrapping covers `createClient`, `createCluster`, `createClientPool` and `createSentinel` when the installed `redis` version exports them. Only the methods above are traced, not every Redis command. `multi()` transactions, `sendCommand()`, pool `execute()` callbacks and clients created directly through `@redis/client` are not automatically instrumented. You can pass an individual borrowed or separately created client to `instrumentClient()` to trace its supported direct commands.

Redis's native OpenTelemetry observability exports metrics; it does not replace these command traces. Future AGI ingests traces, not Redis metrics.

## Attributes and privacy

| Attribute | Value |
| --- | --- |
| `db.system` | `redis` |
| `db.operation.name` | Operation from the table above |
| `fi.span.kind` | `VECTOR_DB` |
| `db.redis.index_name` | Search index name |
| `db.redis.key` | Single string key for hash, JSON and delete operations |
| `db.operation.type` | `vector_search` for a KNN search |
| `db.vector.query.top_k` | `LIMIT.size`, when supplied; not the KNN clause's neighbor count |
| `db.operation.batch_size` | Number of keys in an array passed to `del` |
| `db.response.total_count` | Search response `total` |
| `db.response.returned_rows` | Number of search documents returned |
| `db.redis.num_docs` | Index document count from `ft.info` |

Query text, hash values, JSON content, result documents and vectors are not captured. The legacy `captureQueryVectors` and `captureResultVectors` options remain accepted but have no effect. There is no `captureDocuments` option.

Keys and index names are exported as metadata; keep personal data out of these names or remove these attributes in your collector. `hideInputs` and `hideOutputs` do not remove Redis key or index metadata. Use `traceConfig: { hideErrorDetails: true }` to omit exception messages and stack traces, which can contain server-supplied content. Errors still propagate unchanged to your application and mark the span as an error.

## Troubleshooting

- **No spans with ESM:** create clients through the value returned by `manuallyInstrument()`, or call `instrumentClient()` on an existing client. Imported namespace objects and previously imported factory bindings cannot be rewritten.
- **No spans after setup:** confirm you set the tracer provider and flush or shut it down before a short-lived process exits.
- **Some commands have no spans:** check the supported-method table. This package is not general-purpose Redis command tracing.

## License

Apache-2.0
