# TraceAI Mastra

Send Mastra agent, model, tool, and workflow traces to Future AGI using Mastra's native observability exporter interface. You do not need to patch your agents or add a second tracer provider.

## Install

For Mastra v1, install the integration with these matching observability packages. Upgrade the three Mastra packages together rather than mixing newer observability packages with an older core.

```shell
npm install @traceai/mastra @mastra/core@1.82.0 \
  @mastra/observability@1.23.1 @mastra/otel-exporter@1.5.1 \
  @opentelemetry/api@~1.9.0 @opentelemetry/exporter-trace-otlp-proto@~0.201.1
```

Keep the OpenTelemetry versions above compatible with `@traceai/mastra`'s peer dependencies rather than installing the latest exporter independently.

## Set credentials

Set your Future AGI credentials in the application environment:

```shell
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
```

The package reads `process.env`. If you store these values in an environment file, load it with your application tooling before creating observability.

## Configure tracing

Add `createFIObservability` to your existing Mastra configuration. Use `observability`, not the removed Mastra v0 `telemetry` option.

```typescript
import { Mastra } from "@mastra/core";
import { createFIObservability } from "@traceai/mastra";

export const observability = createFIObservability({
  serviceName: "support-agent",
  projectName: "customer-support",
  projectType: "observe",
});

export const mastra = new Mastra({
  // Keep your existing agents, workflows, storage, and other configuration here.
  observability,
});
```

Run your registered agents or workflows as usual. In Future AGI, open **Observe** and select the `customer-support` project to inspect the trace and its child spans.

The exporter sends OTLP/HTTP protobuf **traces only** to `https://api.futureagi.com/tracer/v1/traces`, using `x-api-key` and `x-secret-key` headers. It sets the `project_name` and `project_type` resource attributes for project routing. An explicit `projectName` takes precedence over `FI_PROJECT_NAME`. If neither is set, `createFIObservability` uses `serviceName`.

Available input and output are included in traces, which can contain prompts, responses, and tool arguments or results. Use Mastra's native `tracingOptions.hideInput` and `hideOutput` for runs that should not capture input/output. Those options do not redact every metadata field, attribute, or error; configure Mastra's [SensitiveDataFilter](https://mastra.ai/reference/observability/tracing/processors/sensitive-data-filter) for field-based redaction.

The exporter also honors Future AGI privacy settings at export time. Set them before creating observability:

```shell
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_ERROR_DETAILS=true
# Optional PII redaction using the shared FI policy:
export FI_PII_REDACTION=true
```

Content hiding covers both native Mastra attributes and Future AGI input/output aliases. Granular `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, and `FI_HIDE_INPUT_IMAGES` conservatively redact complete I/O aliases; `FI_HIDE_EMBEDDING_VECTORS` hides embedding vectors. Content hiding implies error-detail hiding unless `FI_HIDE_ERROR_DETAILS=false` explicitly overrides it. Error types and failure status remain available. Structured credential fields are redacted even when content capture is enabled; this is not a guarantee that arbitrary free text is secret-free.

The supported minimum pairing is `@mastra/core@1.16.0`, `@mastra/observability@1.0.0`, and `@mastra/otel-exporter@1.0.0`. Prefer the current pairing above: the minimum observability runtime lacks cancellation outcomes and some real-agent error signals, so an exporter cannot recover those missing signals. Current runtime outcomes and stop reasons are preserved; cancelled or interrupted executions are never labeled successful when the runtime supplies that outcome.

## Shut down cleanly

After all agent or workflow runs finish, await shutdown before a short-lived script exits:

```typescript
await observability.shutdown();
```

For a long-running application, call this from your application shutdown lifecycle, not after every request. Shutdown closes the exporter after flushing buffered spans.

## Configuration

`createFIObservability(options)` accepts these settings:

| Option | Default | Purpose |
| --- | --- | --- |
| `serviceName` | `"mastra-app"` | OpenTelemetry service name and project fallback when neither `projectName` nor `FI_PROJECT_NAME` is set. |
| `projectName` | `FI_PROJECT_NAME` or `serviceName` | Future AGI project to receive traces; an explicit option overrides the environment. |
| `projectType` | `"observe"` | Project type: `"observe"` or `"experiment"`. |
| `apiKey`, `secretKey` | `FI_API_KEY`, `FI_SECRET_KEY` | Future AGI authentication. |
| `baseUrl` | `FI_BASE_URL` or `https://api.futureagi.com` | Collector base URL; `/tracer/v1/traces` is appended. |
| `endpoint` | Derived from `baseUrl` | Full traces endpoint; overrides `baseUrl`. |
| `resourceAttributes` | — | Additional OpenTelemetry resource attributes. |
| `headers` | — | Additional request headers. |
| `excludeSpanTypes` | `[SpanType.MODEL_CHUNK]` | Span types to omit; pass `[]` to include streaming chunks. Use the package set above; `@mastra/observability` 1.0 does not apply this filter. |
| `timeout` | Exporter default | Export request timeout in milliseconds. |
| `batchSize` | Processor default | Maximum spans per export batch. |

If you already manage a Mastra `Observability` instance, add `createFIMastraExporter(options)` to its `exporters` array instead of creating a second observability instance. Pass `projectName` explicitly to this lower-level factory (or set `FI_PROJECT_NAME`); it does not default the project name to the service name.

## Mastra v0

The deprecated `FITraceExporter` and `isFISpan` exports remain available from `@traceai/mastra/legacy` for existing Mastra v0 applications. They are not root exports and do not work with Mastra v1's removed `telemetry` configuration. For new applications, use the v1 setup above.

See the [Mastra integration guide](https://docs.futureagi.com/docs/integrations/traceai/mastra) for more details.
