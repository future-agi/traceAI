# @traceai/chromadb

Trace Chroma collection operations in Node.js with OpenTelemetry and view their latency, result counts, and errors in Future AGI.

## Install

```bash
npm install chromadb @traceai/chromadb @traceai/fi-core @opentelemetry/api @opentelemetry/context-async-hooks
```

The instrumentor supports the Chroma 1.x, 2.x, and 3.x Node clients. The example below uses Chroma 3.x and a Chroma server at `localhost:8000`. It supplies embeddings directly, so no embedding-provider key is needed.

## Configure tracing

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment using your Future AGI credentials. For a self-hosted deployment, also set `FI_BASE_URL` to your collector base URL.

Save this as `app.mjs`. Explicit module instrumentation works with ESM without an additional import hook. Call it before obtaining collections.

```javascript
import { register, ProjectType } from "@traceai/fi-core";
import { ChromaDBInstrumentation } from "@traceai/chromadb";
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import * as chromadb from "chromadb";

// Set up context propagation for this standalone example.
const contextManager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(contextManager);
const provider = register({
  projectName: "chroma-search",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new ChromaDBInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(chromadb);

try {
  const client = new chromadb.ChromaClient({ host: "localhost", port: 8000 });
  const collection = await client.getOrCreateCollection({ name: "articles" });
  await collection.upsert({
    ids: ["doc1", "doc2"],
    documents: ["Search with vectors", "Store document metadata"],
    embeddings: [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
  });
  const results = await collection.query({
    queryEmbeddings: [[0.1, 0.2, 0.3]],
    nResults: 2,
  });
  console.log(results.ids);
} finally {
  instrumentation.disable();
  await provider.shutdown();
  contextManager.disable();
}
```

```bash
node app.mjs
```

Open the `chroma-search` project in [Future AGI](https://app.futureagi.com) to inspect the `chroma upsert` and `chroma query` spans.

If you already have an OpenTelemetry provider, reuse it with `instrumentation.setTracerProvider(provider)` rather than creating another provider. Keep one instrumentor instance per process.

## Automatic instrumentation

For CommonJS, create the instrumentor before the first `require("chromadb")`. For ESM automatic instrumentation, load the [OpenTelemetry instrumentation hook](https://www.npmjs.com/package/@opentelemetry/instrumentation) before importing Chroma. Explicit `manuallyInstrument(chromadb)` is the simpler option when you control application startup.

Both paths cover exported collection classes and collections returned by client factories, including `getCollection`, `createCollection`, and `getOrCreateCollection`. Chroma 3.x handles from `client.collection(id)` are also covered. `disable()` restores methods; `enable()` restores tracing for already-discovered collections without stacking wrappers.

## Traced operations

| Method | Span name |
| --- | --- |
| `add` | `chroma add` |
| `query` | `chroma query` |
| `search` (when available in your Chroma client) | `chroma search` |
| `get` | `chroma get` |
| `update` | `chroma update` |
| `upsert` | `chroma upsert` |
| `delete` | `chroma delete` |
| `count` | `chroma count` |
| `peek` | `chroma peek` |

Each operation emits one `CLIENT` span with `db.system=chromadb`, `db.operation.name`, and `fi.span.kind=VECTOR_DB`. Internal delegation, such as `peek` calling `get`, does not create a second database span. Factory calls discover collection implementations but do not emit database spans themselves.

Operation attributes include the collection name when available, query result limit, result count, and write count/dimensions. `search` records the number of searches and total returned IDs across the batch. A lightweight collection handle does not know the collection name, so its name is recorded as `unknown`.

## Data captured

The instrumentor does not record document contents or embedding vectors. The existing `captureDocuments`, `captureQueryVectors`, and `captureResultVectors` options remain accepted for compatibility but do not enable content capture.

Query `where` filters and collection names are recorded; avoid putting secrets in either. Error messages can also contain application data. To hide error messages and exception details, configure the instrumentor with:

```javascript
const instrumentation = new ChromaDBInstrumentation({
  traceConfig: { hideErrorDetails: true },
});
```

## Troubleshooting

- **No collection spans:** call `manuallyInstrument` on the same Chroma module your application uses, before obtaining collections. This is especially important with ESM or bundled applications.
- **Spans are separate from their parent request:** configure an OpenTelemetry context manager, or use your existing Node SDK's context management.
- **Traces do not reach Future AGI:** check both `FI_API_KEY` and `FI_SECRET_KEY`, the collector endpoint, and that you flush or shut down the provider before exiting.

## Development

Build the workspace dependencies before building this package. `pnpm test` runs unit tests; `pnpm test:runtime` exercises the installed Chroma client against a loopback fixture and exports OTLP spans to a loopback receiver. The compatibility workflow also tests CJS/ESM imports and automatic/manual instrumentation across supported major versions.

## License

Apache-2.0
