# @traceai/vercel

Send Vercel AI SDK's native OpenTelemetry spans to Future AGI. This package adds Future AGI span classifications and normalizes older AI SDK message attributes; it does not wrap model calls or create additional spans.

## Compatibility

- AI SDK 6 (starting at 6.0.0): enable `experimental_telemetry` on each call. Aborted streams and mid-stream transport errors can leave native spans unfinished; use AI SDK 7 if you need complete traces for those calls. Flushing cannot recover a span the SDK never ends.
- AI SDK 7: register the official `@ai-sdk/otel` integration once, and use `telemetry` on each call. Use `OpenTelemetry`, not both it and `LegacyOpenTelemetry`.
- The Future AGI processors extend OpenTelemetry SDK **1.x**. Keep the SDK/exporter versions below together; do not mix these processors with SDK 2.x.
- Use a Node.js server, not an Edge or browser runtime. The AI SDK 7 setup below requires Node.js 22 or newer.

## Install

```bash
npm install @traceai/vercel ai@7.0.159 @ai-sdk/openai@4.0.106 @ai-sdk/otel@1.0.159 \
  @opentelemetry/api@1.9.1 @opentelemetry/sdk-trace-node@1.30.1 \
  @opentelemetry/resources@1.30.1 @opentelemetry/exporter-trace-otlp-http@0.57.2
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and your own `OPENAI_API_KEY` in the server environment. Never expose these credentials in client code.

## Register tracing once

```javascript
// tracing.mjs
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { Resource } from '@opentelemetry/resources';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { FIBatchSpanProcessor, isFISpan } from '@traceai/vercel';
import { registerTelemetry } from 'ai';
import { OpenTelemetry } from '@ai-sdk/otel';

const apiKey = process.env.FI_API_KEY;
const secretKey = process.env.FI_SECRET_KEY;
if (!apiKey || !secretKey) throw new Error('Set FI_API_KEY and FI_SECRET_KEY.');

export const provider = new NodeTracerProvider({
  resource: new Resource({
    'service.name': 'vercel-app',
    project_name: 'vercel-app',
    project_type: 'observe',
  }),
});
provider.addSpanProcessor(new FIBatchSpanProcessor({
  exporter: new OTLPTraceExporter({
    url: 'https://api.futureagi.com/tracer/v1/traces',
    headers: { 'X-Api-Key': apiKey, 'X-Secret-Key': secretKey },
  }),
  spanFilter: isFISpan,
}));
provider.register();
registerTelemetry(new OpenTelemetry());
```

If your app already has an OpenTelemetry provider, add the processor to that provider instead of registering another. Do not additionally instrument the same provider calls with `@traceai/openai`.

## Make a model call

```javascript
// generate.mjs
import { provider } from './tracing.mjs';
import { generateText } from 'ai';
import { openai } from '@ai-sdk/openai';

try {
  const result = await generateText({
    model: openai.chat('gpt-4o-mini'),
    prompt: 'Suggest three names for a neighborhood book club.',
    telemetry: { recordInputs: false, recordOutputs: false },
  });
  console.log(result.text);
} finally {
  // A CLI should drain its queue before exit. In a long-running server,
  // shut down the provider only when the server stops, not after each request.
  await provider.shutdown();
}
```

Run `node generate.mjs`, then open **Observe → vercel-app** in Future AGI. You can inspect model calls, tool executions, timing, token usage, and native error status without creating duplicate model-call spans.

For streaming calls, finish consuming the stream before flushing or shutting down tracing.

Input/output recording is disabled above. Enable it only for data you are allowed to store.

The processors also read Future AGI privacy settings when constructed. Set them before initializing tracing:

- `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true` remove input and output payloads, including native and normalized messages and tool data.
- `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, and `FI_HIDE_INPUT_IMAGES` also remove the corresponding full serialized payloads. This deliberately removes more than the selected modality so another copy cannot expose it.
- `FI_HIDE_EMBEDDING_VECTORS=true` removes native and normalized vectors without hiding the embedding text.
- `FI_HIDE_ERROR_DETAILS=true` removes error messages, exception stack traces, and status descriptions, retaining error status and exception type. It defaults to true when input/output, message, or text hiding is enabled; an explicit `false` overrides that default.

Native request-header attributes are always excluded, including custom authentication headers. These controls apply only to exports through the Future AGI processors; another processor on the same provider has its own policy. Avoid putting sensitive data in custom span names or metadata.

### Existing AI SDK 6 applications

Keep your AI SDK 6 provider dependencies. Use the same provider/exporter/processor setup, but omit `registerTelemetry` and `@ai-sdk/otel`. Replace the call's `telemetry` option with:

```typescript
experimental_telemetry: {
  isEnabled: true,
  recordInputs: false,
  recordOutputs: false,
}
```

### Next.js

Initialize tracing from Next.js's `instrumentation.ts` `register()` hook, guarded by `process.env.NEXT_RUNTIME === 'nodejs'`. Do not call registration from `_app.tsx` or an API handler. Add the processor to your existing `@vercel/otel` configuration if you already use it, keeping its OpenTelemetry dependencies on SDK 1.x.

## API

```typescript
new FISimpleSpanProcessor({ exporter, spanFilter? })
new FIBatchSpanProcessor({ exporter, spanFilter?, config? })
```

- `exporter`: an OpenTelemetry 1.x-compatible `SpanExporter`.
- `spanFilter`: optional predicate on the normalized, privacy-filtered span. `isFISpan` exports recognized AI spans and excludes unrelated HTTP/server spans.
- `config`: optional OpenTelemetry `BufferConfig` for the batch processor.
- `forceFlush()` and `shutdown()`: drain queued exports. Use the provider's lifecycle methods in application code.

AI SDK 7's native GenAI messages, model, provider, usage, and tool fields are preserved unless hidden by the privacy settings. Reranking, speech, transcription, streaming transcription, and decision spans are classified as well. AI SDK 6's older `ai.*` fields are normalized without replacing existing GenAI values. Tool-call IDs and every tool result are retained.

## Troubleshooting

- **No AI SDK 7 spans:** register `OpenTelemetry` from `@ai-sdk/otel` once. `experimental_telemetry` alone is the AI SDK 6 setup, not the SDK 7 setup.
- **No AI SDK 6 spans:** set `experimental_telemetry.isEnabled` to `true`.
- **Rejected exports:** check both Future AGI headers, the full `/tracer/v1/traces` endpoint, and the project resource attributes.
- **Processor type errors:** use the pinned OpenTelemetry 1.x SDK/exporter combination above.
- **Missing final spans in a CLI or job:** await `provider.shutdown()` before exit.
