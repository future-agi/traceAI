# Flue tracing with Future AGI

Send Flue's native agent, model and tool spans to a Future AGI Observe project without maintaining an exporter or privacy helper.

## Install

Use Node.js 22.19 or later and matching Flue runtime and OpenTelemetry packages. Supported Flue versions are 2.2.2 through 2.3.x.

```bash
npm install @traceai/flue @flue/runtime@2.3.0 @flue/opentelemetry@2.3.0
```

## Configure

```bash
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
export FI_PROJECT_NAME="weather-agent"
# Self-hosted installations: an origin without a path.
# export FI_BASE_URL="https://your-futureagi-host"
```

Create one small setup module and import it before your agent runs:

```ts
// src/telemetry.ts
import { configure } from '@traceai/flue';
export const tracing = configure();
```

Import `./telemetry.ts` from `src/app.ts`, and `../telemetry.ts` from your agent module when using `flue run`. The CLI loads the agent module, not `app.ts`.

Do not also register `createOpenTelemetryInstrumentation()` yourself. This package calls the public `instrument()` API with Flue's official adapter as the only span producer. Repeated identical `configure()` calls return the same handle; changing settings requires shutdown first. Missing credentials or invalid settings throw before registration.

The adapter uses a private trace provider. It does not replace a global provider, install resource detectors, register signal handlers or export metrics/logs. It adds `fi.span.kind` for agent/model/tool/internal spans and maps `gen_ai.conversation.id` to `session.id`, preserving native trace IDs and parent links. Authentication uses both `X-Api-Key` and `X-Secret-Key`; project routing uses resource attributes `project_name` and `project_type=observe`.

## Privacy

Content is off by default. Prompts, responses, tool arguments/results, exception messages and stack traces are excluded by the native content policy. Host/process resource metadata is not collected. Agent/model/tool names, opaque conversation and instance IDs, model endpoint hosts, timings, usage and error types remain visible; do not put personal information in those identifiers.

Opt in with `captureContent: true` or `FI_CAPTURE_CONTENT=1`. Stack traces remain off, and Flue applies its native content budget. A native `transform` can redact content before export:

```ts
const tracing = configure({
  captureContent: true,
  transform: (content, scope) =>
    scope.contentType === 'exception_message' ? undefined : content,
});
```

An explicit `captureContent: false` overrides the environment. Content transforms run only when capture is enabled. See [Flue's content policy](https://flueframework.com/docs/ecosystem/tooling/opentelemetry/) for supported scope values.

## Flush and stop

```ts
try {
  // Run your agent and await its result.
} finally {
  await tracing.shutdown();
}
```

`flush()` delivers ended spans without stopping tracing. `shutdown()` unregisters the adapter, ends unfinished native spans and flushes once. Both reject on failure and bound the caller's wait with `timeoutMillis` (default 5000; allowed 1–60000).

The adapter also flushes on submission settlement, participates in `flue run` disposal and attempts shutdown on Node's `beforeExit`. Background export failures go through OpenTelemetry diagnostics without logging credentials or raw exporter errors.

**For generated Node servers, drain active work and let exports finish before stopping the process.** Flue can call `process.exit()` during shutdown, so SIGTERM alone does not guarantee the last export. No tracing library can recover queued spans after SIGKILL or a crash. When you own the server lifecycle, await `tracing.shutdown()` before exiting.

## Options

| Option | Default |
| --- | --- |
| `projectName` | `FI_PROJECT_NAME`, then `flue-agent` |
| `apiKey`, `secretKey` | `FI_API_KEY`, `FI_SECRET_KEY` (both required) |
| `baseUrl` | `FI_BASE_URL`, then `https://api.futureagi.com` |
| `serviceName` | `OTEL_SERVICE_NAME`, then the project name |
| `captureContent` | `FI_CAPTURE_CONTENT=1/true/yes/on`, otherwise false |
| `transform` | No additional redaction; stack traces always excluded |
| `timeoutMillis` | 5000 |

Only OTLP/HTTP protobuf traces are sent, uncompressed, to `/tracer/v1/traces`. This is a Node.js integration, not a Cloudflare Worker adapter. It does not add spans for operations Flue does not trace.
