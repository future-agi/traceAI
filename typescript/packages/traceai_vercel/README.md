# @traceai/vercel

Future Instrumentation (FI) integration for the **Vercel AI SDK**.

This package provides Node.js auto-instrumentation for AI SDK v5, v6 and v7, plus the existing manual `SpanProcessor` adapters. Auto-instrumentation covers `generateText`, `streamText`, `generateObject`, `streamObject`, `embed`, `embedMany`, and executed tools without adding telemetry options to application calls.

## Installation

```bash
npm install @traceai/vercel
# or
yarn add @traceai/vercel
# or
pnpm add @traceai/vercel
```

## Zero-code integration entry

A launcher can call the zero-argument `enableVercelInstrumentation()` (also exported as `enable`) **before loading `ai`**. Repeated calls return the same instrumentor. Enabling does not configure or replace an OpenTelemetry provider, install exporters, make network requests, or take over signal handlers. The launcher/application must configure the provider, context manager, exporter, and export-time privacy boundary separately.

```typescript
// Node.js CommonJS preload, after configuring the application's OTel provider.
const { enableVercelInstrumentation } = require("@traceai/vercel");
enableVercelInstrumentation();

// Unchanged application code, loaded after the preload.
const { generateText } = require("ai");
// await generateText({ model, prompt: "Hello" }); // no experimental_telemetry
```

For native ESM, the launcher must also install OpenTelemetry's `@opentelemetry/instrumentation/hook.mjs` import hook **before importing `ai`**. This package registers the matching instrumentation hooks but does not install a process-wide ESM loader itself. The entry is ready for `@traceai/auto` to call; this package change does not wire that agent or change its integration catalog.

- v5/v6: enables SDK-native telemetry using a fail-open FI tracer adapter. Existing telemetry settings and explicit opt-outs are retained; inputs/options are not mutated.
- v7: registers the native telemetry integration, preserves existing global/per-call integrations and their receivers, and composes execution wrappers without dropping their results. Local integrations keep precedence over global ones.
- Model/embedding/tool spans carry canonical `gen_ai.span.kind` and the legacy FI kind. Model execution is placed in the corresponding context for transport-layer de-duplication. FI context attributes and `TraceConfig` masking are applied.
- The public SDK promises, result objects and helper methods remain the originals. No handlers are attached to public promises, preserving Node's unhandled-rejection events and default exit behavior. v7 object/embedding spans start only when the SDK emits `onStart`, after initial validation, while retaining model context for transport de-duplication.
- Streams are not eagerly consumed; a demand-driven internal stream adapter closes otherwise orphaned v5/v6 native spans and v7 callback spans on observed raw stream errors. Embedding vectors remain in application results, not exported attributes.

`VercelAIInstrumentation` is also exported for OTel registries that construct an instrumentor and set its tracer provider. Its `manuallyInstrument(namespace, version)` helper **returns** the patched namespace; callers must use that return value for immutable CJS/ESM exports. A process should use one instrumentor; disable-after-enable/reconfiguration is not supported by the singleton entry.

## Manual processor alternative

The older processor-only mode below is still supported. Unlike the auto-instrumentation entry, **this mode requires** `experimental_telemetry: { isEnabled: true }` on AI SDK v5/v6 calls. Do not install a second FI exporter for spans already exported by the agent.

Send Vercel AI SDK's native OpenTelemetry spans to Future AGI. This package adds Future AGI span classifications and normalizes older AI SDK message attributes; it does not wrap model calls or create additional spans.

### Compatibility

- AI SDK 6 (starting at 6.0.0): enable `experimental_telemetry` on each call. Aborted streams and mid-stream transport errors can leave native spans unfinished; use AI SDK 7 if you need complete traces for those calls. Flushing cannot recover a span the SDK never ends.
- AI SDK 7: register the official `@ai-sdk/otel` integration once, and use `telemetry` on each call. Use `OpenTelemetry`, not both it and `LegacyOpenTelemetry`.
- The Future AGI processors extend OpenTelemetry SDK **1.x**. Keep the SDK/exporter versions below together; do not mix these processors with SDK 2.x.
- Use a Node.js server, not an Edge or browser runtime. The AI SDK 7 setup below requires Node.js 22 or newer.

### Install

```bash
npm install @traceai/vercel ai@7.0.161 @ai-sdk/openai@4.0.107 @ai-sdk/otel@1.0.161 \
  @opentelemetry/api@1.9.1 @opentelemetry/sdk-trace-node@1.30.1 \
  @opentelemetry/resources@1.30.1 @opentelemetry/exporter-trace-otlp-http@0.57.2
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and your own `OPENAI_API_KEY` in the server environment. Never expose these credentials in client code.

### Register tracing once

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

### Make a model call

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

#### Existing AI SDK 6 applications

Keep your AI SDK 6 provider dependencies. Use the same provider/exporter/processor setup, but omit `registerTelemetry` and `@ai-sdk/otel`. Replace the call's `telemetry` option with:

```typescript
experimental_telemetry: {
  isEnabled: true,
  recordInputs: false,
  recordOutputs: false,
}
```

#### Next.js

Initialize tracing from Next.js's `instrumentation.ts` `register()` hook, guarded by `process.env.NEXT_RUNTIME === 'nodejs'`. Do not call registration from `_app.tsx` or an API handler. Add the processor to your existing `@vercel/otel` configuration if you already use it, keeping its OpenTelemetry dependencies on SDK 1.x.

### API

```typescript
new FISimpleSpanProcessor({ exporter, spanFilter? })
new FIBatchSpanProcessor({ exporter, spanFilter?, config? })
```

- `exporter`: an OpenTelemetry 1.x-compatible `SpanExporter`.
- `spanFilter`: optional predicate on the normalized, privacy-filtered span. `isFISpan` exports recognized AI spans and excludes unrelated HTTP/server spans.
- `config`: optional OpenTelemetry `BufferConfig` for the batch processor.
- `forceFlush()` and `shutdown()`: drain queued exports. Use the provider's lifecycle methods in application code.

AI SDK 7's native GenAI messages, model, provider, usage, and tool fields are preserved unless hidden by the privacy settings. Reranking, speech, transcription, streaming transcription, and decision spans are classified as well. AI SDK 6's older `ai.*` fields are normalized without replacing existing GenAI values. Tool-call IDs and every tool result are retained.

The simple processor enriches the underlying Vercel AI SDK spans with FI semantic conventions and forwards them to the required exporter. `isFISpan(span)` returns `true` when the span already contains FI semantic conventions.

### Troubleshooting

- **No AI SDK 7 spans:** register `OpenTelemetry` from `@ai-sdk/otel` once. `experimental_telemetry` alone is the AI SDK 6 setup, not the SDK 7 setup.
- **No AI SDK 6 spans:** set `experimental_telemetry.isEnabled` to `true`.
- **Rejected exports:** check both Future AGI headers, the full `/tracer/v1/traces` endpoint, and the project resource attributes.
- **Processor type errors:** use the pinned OpenTelemetry 1.x SDK/exporter combination above.
- **Missing final spans in a CLI or job:** await `provider.shutdown()` before exit.

## Validation

The offline test matrix uses the real `ai` packages **5.0.283**, **6.0.320**, and **7.0.158**, with each version's mock language/embedding provider. No API key, external service, or model download is required.

```bash
pnpm --filter @traceai/vercel test
pnpm --filter @traceai/vercel build
# Fresh Node processes test actual lazy CJS/ESM hooks and the built exports:
node packages/traceai_vercel/src/__tests__/runtime.cjs 5 cjs
node packages/traceai_vercel/src/__tests__/runtime.cjs 5 esm
# Repeat with 6 and 7.
# Fresh-process rejection probes (repeat for embedMany and generateObject):
node packages/traceai_vercel/src/__tests__/rejections.cjs embed baseline unhandled
node packages/traceai_vercel/src/__tests__/rejections.cjs embed traced unhandled
# Both commands above must exit 1 with "provider failed", never survive silently.
node packages/traceai_vercel/src/__tests__/rejections.cjs embed traced caught
node packages/traceai_vercel/src/__tests__/rejections.cjs embed traced late
# caught/late must exit 0; late verifies original-promise rejection events.
```

The Jest regressions compare raw transport failures and unread streams across both streaming APIs and all three majors, tracking started versus ended spans. They also verify handler-free promise identity, provider errors, and early v7 validation errors without orphan spans. The standalone rejection probes deliberately run outside Jest so its rejection handlers cannot conceal changes to Node's default process exit behavior.

`registry.test.ts` additionally launches fresh Node processes through the real agent's zero-code register entry and lazy module hooks, using current TypeScript sources rather than stale build output. Across v5/v6 native telemetry and v7 callbacks, `generateText` and `streamText` receive a loopback provider's 401 echo of an API key absent from initialization's environment seeds. Direct and intermediate-native-client variants must match no-agent errors, callbacks and request counts, export exactly one model span per call with no duplicate wire spans, and scrub the key from every serialized OTLP surface, including enclosing and later unrelated application spans. The application-owned exporter's original attributes, exception events, resources, scopes and links remain unchanged, and ordinary content remains captured by default. The serial child runtimes inherit the guarded test suite's process-tree host budget; temporary applications are removed after each run.

The auto-instrumentation regression configuration leaves content hiding off. The manual quick-start above deliberately disables input/output recording; enabling auto-instrumentation does not remove explicit recording opt-outs or FI hiding settings.

## Known limitations

As of **2026-10-30**:

- Auto-instrumentation targets Node.js, not browsers/Edge isolates. AI SDK v7 itself requires Node.js 22 or newer. CJS loading of its ESM-only distribution also requires a Node release that supports `require(esm)`; native ESM is the recommended path.
- Preloading is required for complete coverage. Previously captured function references, bundled/inlined SDK copies, unsupported majors/prereleases, and runtimes without working module hooks may not be patched; failures leave the application running. The v7 global callback fallback alone does not intercept per-call integration overrides or bind object/embedding provider context. The agent's wire layer is the fallback in unsupported environments.
- Spans measure SDK/library lifecycle, including provider decoding, SDK parsing and retries, not raw transport-byte timing. Use the separate wire layer for transport timing; no transport-timing claim is made for these library spans.
- An unread/abandoned stream is not drained or forced closed to finish telemetry. Final spans require SDK completion, an observed failure, or cancellation reaching the underlying stream. Raw-error cleanup in v5/v6 and v7 relies on patching the SDK's private object-mode `baseStream` field, verified in the pinned releases; the v7 global-callback-only fallback does not provide this cleanup. A future internal layout change will skip that cleanup rather than change application results.
- v7 callback-only outstanding-call bookkeeping is bounded to 1,024 operations; an orphan evicted at that limit ends as incomplete. Captured callback JSON is bounded (2,048 nodes, depth 12, 64 KiB of string content); binary fields and vectors are omitted. Complex multimodal content can therefore be incomplete in library attributes.
- This adapter applies FI masking but is **not** a standalone credential/media sanitizer. The agent must apply the contract's shared export-time sanitizer to every exported span. Do not treat calling `enable()` alone as full agent initialization, privacy enforcement, collector configuration, or agent integration wiring.
- The legacy processor classes continue to use the OTel 1.x processor API. The auto adapter uses the global OTel API and does not require those processors on a 2.x provider. AI SDK v7 deprecates `generateObject`/`streamObject`; they remain covered in the pinned release.
- **2026-10-30 — v5/v6 embedding usage:** native `ai.usage.tokens` is not mapped before native `ai.*` attributes are removed. Provider-reported embedding token usage is therefore absent from embedding and enclosing chain spans, although application results retain it.
- **2026-10-30 — v7 batch embedding dimensions:** when `embedMany` completion supplies its matrix under `embedding`, `gen_ai.embeddings.dimension.count` records the outer array length (batch size), not the first vector's dimensionality. Application vectors are unchanged and are not exported.
- **2026-10-30 — v7 model identity:** completion callbacks can replace `gen_ai.request.model` with the resolved response model without setting `gen_ai.response.model`. The application's requested model and returned response remain unchanged.
- **2026-10-30 — v7 ordinary structured-output fields:** callback serialization omits fields named `data`, `bytes`, `image`, `audio`, `file`, `embedding` or `embeddings`, even when those names contain ordinary JSON rather than binary media/vectors. Application objects are unchanged, but exported structured output can be incomplete.
- **Tool-result history after reconciliation:** canonical input-message normalization now reads current `output` fields with legacy `result` fallback and retains every tool result. Tool-call mapping shares the same formatter for prompt and response messages, preferring non-nullish `input` over `args`. Application tool history is unchanged.
- **2026-10-30 — S5 privacy scope:** credential observation and process-wide echo scrubbing belong to the core agent, including when model coverage suppresses a wire span. This package does not inspect provider state or maintain credential groups/inheritance, and has no extra non-transport credentials to register. Enabling only this adapter or using manual processors without the agent's transport hooks does not discover runtime provider credentials; initialization-seeded FI/environment values are the registry's baseline. Unmarked credentials that never cross an instrumented transport, or application-transformed secrets (encoding, splitting or numeric re-typing), may not be recognized. Register known non-transport values with the core `registerSecret(value)` API; use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS` to withhold sensitive content/error details rather than relying on recognition of transformed values.

## Support

For support, please open an issue in our [GitHub repository](https://github.com/future-agi/traceAI/issues).
