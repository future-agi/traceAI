# Apollo GraphQL tracing

Trace Apollo Server requests, GraphQL operations and resolvers in Future AGI using the maintained OpenTelemetry HTTP and GraphQL instrumentations. This adapter configures their exporter and privacy policy; it does not create a second set of Apollo spans.

## Setup

Use Apollo Server 5, GraphQL 16.11 or later in the 16.x line, and Node.js 22.15 or later. The current Apollo dependency tree cannot be installed with strict engine checks on Node 20. Server 4.13 remains compatible for migrations, but is end-of-life and should not be used for new applications. This package targets the server executing resolvers, not Apollo Gateway or Router.

```sh
npm install @traceai/apollo-graphql @apollo/server@5 graphql@16
export FI_API_KEY=...
export FI_SECRET_KEY=...
export FI_PROJECT_NAME=my-graphql-api
# Optional for self-hosting:
# export FI_BASE_URL=https://your-futureagi-origin
```

GraphQL 17 is not supported by this adapter, Apollo Server 5's peer range or the upstream GraphQL instrumentation. Keep `graphql@16`; do not bypass peer dependency checks to install 17. `configure()` rejects unsupported GraphQL versions before starting tracing.

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

A static import beside Apollo imports is not a substitute for preloading. Apollo and GraphQL must not load before tracing. The adapter refreshes Node's ESM HTTP exports after upstream instrumentation patches HTTP, so Apollo Server 5 standalone retains the HTTP request parent. Return an extensible object from your Apollo context factory: the upstream GraphQL instrumentation adds execution metadata to it, so a frozen context causes requests to fail.

## Configuration

`configure(options?)` owns one tracing SDK per process. Do not also start NodeSDK, auto-instrument HTTP/GraphQL elsewhere, or call `configure` twice. If you already own an SDK, the exported `FiSanitizingExporter` can wrap its exporter, but you own instrumentation registration, ESM HTTP binding synchronization, resource privacy and lifecycle in that setup.

| Option | Default |
| --- | --- |
| `projectName` | `FI_PROJECT_NAME`, otherwise `apollo-graphql` |
| `baseUrl` | `FI_BASE_URL`, otherwise `https://api.futureagi.com` |
| `apiKey`, `secretKey` | `FI_API_KEY`, `FI_SECRET_KEY`; both required |
| `depth` | `1`: top-level resolver paths only; positive integer. List indexes also count as path components, so nested fields under lists can be excluded at lower depths. |
| `captureContent` | `FI_APOLLO_CAPTURE_CONTENT === 'true'`, otherwise `false` |

The destination is `{baseUrl}/tracer/v1/traces`, with `X-Api-Key` and `X-Secret-Key` headers. Unset `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` before `configure()`; otherwise startup refuses to forward another collector's credentials. `project_name`, `project_type=observe` and `service.name` are resource attributes. No automatic host/process/environment resource detection, metrics exporter or log exporter is enabled.

`forceFlush()` waits for completed spans, including in-flight background exports. Call `shutdown()` **after** `server.stop()` to drain requests before flushing. Both resolve to `false` when export fails and do not log the error payload. Shutdown is idempotent. There are no automatic signal handlers in this package. The HTTP exporter's 3-second timeout is a socket inactivity timeout, not a wall-clock deadline; the batch processor has a 4-second export timeout.

## Trace shape and privacy

An HTTP server span parents parsing, validation and operation execution. Apollo's operation cache can omit parsing and validation spans. Resolver spans are children of execution; application spans started inside a resolver inherit its context. GraphQL result errors mark the operation ERROR, including partial success. Invalid requests that never execute do not have an operation span. Startup schema parsing can produce a separate trace. GraphQL spans are not LLM spans and carry no invented model usage.

The HTTP/GraphQL exporter allowlist keeps operation and field metadata, HTTP method/status, timing, trace identity and exception type. It removes query source, error/status messages, stacks, URLs, peers, unknown attributes, unknown events and link attributes. Operation names, schema field names and paths remain visible; do not put secrets in those names.

With `captureContent: true`, query source and exception/status messages are also exported. The upstream `allowValues: false` setting masks string (including block-string), integer and float literals only; enum, Boolean and null literals remain visible. For example, `decision(flag: true, value: PRIVATE_ENUM)` retains both argument values. This is partial masking, not full query redaction. Variable-value attributes and stack traces remain excluded, but error messages may contain arbitrary application data, including argument or variable values. Keep content capture off if query literals or error messages can contain sensitive data.

`FiSanitizingExporter` forwards opted-in source as supplied; it does not mask literals itself. If you wrap your own exporter instead of using `configure()`, configure source masking in your instrumentation too.

Application-created spans and spans from other instrumentations are **not filtered**. Configure their privacy independently. Sanitization failure drops the affected export batch instead of sending the original payload.
