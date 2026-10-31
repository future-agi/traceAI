# Apollo GraphQL tracing

Trace Apollo Server requests, GraphQL operations and resolvers in Future AGI using the maintained OpenTelemetry HTTP and GraphQL instrumentations. This adapter configures their exporter and privacy policy; it does not create a second set of Apollo spans.

## Setup

Use Apollo Server 5, GraphQL 16.11 or later in the 16.x line, and Node.js 20.6 or later. Node.js 22 or later is recommended. Server 4.13 remains compatible for migrations, but is end-of-life and should not be used for new applications. This package targets the server executing resolvers, not Apollo Gateway or Router.

```sh
npm install @traceai/apollo-graphql @apollo/server@5 graphql@16
export FI_API_KEY=...
export FI_SECRET_KEY=...
export FI_PROJECT_NAME=my-graphql-api
# Optional for self-hosting:
# export FI_BASE_URL=https://your-futureagi-origin
```

Create `instrumentation.mjs`:

```js
import { configure } from '@traceai/apollo-graphql';
export const telemetry = configure();
```

Create `server.mjs`:

```js
import { ApolloServer } from '@apollo/server';
import { startStandaloneServer } from '@apollo/server/standalone';
import { telemetry } from './instrumentation.mjs';

const server = new ApolloServer({
  stopOnTerminationSignals: false,
  typeDefs: 'type Query { hello: String! }',
  resolvers: { Query: { hello: () => 'Hello from GraphQL' } },
});
const { url } = await startStandaloneServer(server, { listen: { port: 4000 } });
console.log(`GraphQL server: ${url}`);

let stopping = false;
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, async () => {
    if (stopping) return;
    stopping = true;
    try { await server.stop(); }
    finally {
      if (!await telemetry.shutdown()) console.error('Telemetry export failed');
    }
  });
}
```

Preload instrumentation **before** the application module graph:

```sh
node --import ./instrumentation.mjs server.mjs
```

A static import beside Apollo imports is not a substitute for preloading. Apollo and GraphQL must not load before tracing. The adapter refreshes Node's ESM HTTP exports after upstream instrumentation patches HTTP, so Apollo Server 5 standalone retains the HTTP request parent.

## Configuration

`configure(options?)` owns one tracing SDK per process. Do not also start NodeSDK, auto-instrument HTTP/GraphQL elsewhere, or call `configure` twice. If you already own an SDK, the exported `FiSanitizingExporter` can wrap its exporter, but you own instrumentation registration, ESM HTTP binding synchronization, resource privacy and lifecycle in that setup.

| Option | Default |
| --- | --- |
| `projectName` | `FI_PROJECT_NAME`, otherwise `apollo-graphql` |
| `baseUrl` | `FI_BASE_URL`, otherwise `https://api.futureagi.com` |
| `apiKey`, `secretKey` | `FI_API_KEY`, `FI_SECRET_KEY`; both required |
| `depth` | `1`: top-level resolver spans only; positive integer |
| `captureContent` | `FI_APOLLO_CAPTURE_CONTENT === 'true'`, otherwise `false` |

The destination is `{baseUrl}/tracer/v1/traces`, with `X-Api-Key` and `X-Secret-Key` headers. `project_name`, `project_type=observe` and `service.name` are resource attributes. No automatic host/process/environment resource detection, metrics exporter or log exporter is enabled.

`forceFlush()` exports completed spans. Call `shutdown()` **after** `server.stop()` to drain requests before flushing. Both resolve to `false` when export fails and do not log the error payload. Shutdown is idempotent. There are no automatic signal handlers in this package. Export attempts time out after 3 seconds; the batch processor has a 4-second export timeout.

## Trace shape and privacy

An HTTP server span parents parsing, validation and operation execution. Apollo's operation cache can omit parsing and validation spans. Resolver spans are children of execution; application spans started inside a resolver inherit its context. GraphQL result errors mark the operation ERROR, including partial success. Invalid requests that never execute do not have an operation span. Startup schema parsing can produce a separate trace. GraphQL spans are not LLM spans and carry no invented model usage.

The HTTP/GraphQL exporter allowlist keeps operation and field metadata, HTTP method/status, timing, trace identity and exception type. It removes query source, error/status messages, stacks, URLs, peers, unknown attributes, unknown events and link attributes. Operation names, schema field names and paths remain visible; do not put secrets in those names.

With `captureContent: true`, masked query source and exception/status messages are also exported. Literal values remain masked by upstream `allowValues: false`; variable values and stack traces remain excluded. Error messages may contain arbitrary application data, so enable content only when appropriate.

Application-created spans and spans from other instrumentations are **not filtered**. Configure their privacy independently. Sanitization failure drops the affected export batch instead of sending the original payload.
