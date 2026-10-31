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

## Manual processor alternative (Next.js)

The older processor-only mode below is still supported. Unlike the auto-instrumentation entry, **this mode requires** `experimental_telemetry: { isEnabled: true }` on AI SDK v5/v6 calls. Do not install a second FI exporter for spans already exported by the agent.

### 1. Create a one-time `instrumentation.ts`

```typescript
// instrumentation.ts – Next.js invokes register once at server startup.
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore   @vercel/otel ships without types
import { registerOTel } from "@vercel/otel";
import type { SpanExporter } from "@opentelemetry/sdk-trace-base";

import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { FISimpleSpanProcessor, isFISpan } from "@traceai/vercel";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import { Metadata } from "@grpc/grpc-js";

diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

export function register() {
  registerOTel({
    attributes: {
      "service.name": "vercel-project",
      project_name: "vercel-project",
      project_type: "observe",
    },
    spanProcessors: [
      new FISimpleSpanProcessor({
        exporter: (() => {
          const metadata = new Metadata();
          metadata.set("x-api-key", process.env.FI_API_KEY as string);
          metadata.set("x-secret-key", process.env.FI_SECRET_KEY as string);

          return new OTLPTraceExporter({
            url: "grpc://grpc.futureagi.com",
            metadata,
          }) as unknown as SpanExporter;
        })(),

        // Export only FI spans produced by the Vercel AI SDK
        spanFilter: isFISpan,
      }),
    ],
  });

  diag.info("OTLP gRPC span processor registered via @vercel/otel");
}
```

### 2. Use the tracer in your API / Route handlers

```typescript
// pages/api/story.ts
import type { NextApiRequest, NextApiResponse } from "next";

import { generateText } from "ai";
import { openai } from "@ai-sdk/openai";

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  const result = await generateText({
    model: openai("gpt-4o-mini"),
    prompt: "Say hello from a traced function!",
    experimental_telemetry: { isEnabled: true },
  });

  res.status(200).json({ text: result.text });
}
```

That’s it! Every invocation will emit FI-compliant OpenTelemetry spans you can inspect in the TraceAI dashboard.

## Environment variables

```bash
# TraceAI telemetry credentials
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
```

## API

### `FISimpleSpanProcessor`

```ts
new FISimpleSpanProcessor({ exporter, spanFilter? })
```

Wraps the underlying Vercel AI SDK spans with FI semantic conventions and forwards them to the provided exporter.

**Parameters**

• `exporter` **(required)** – any OpenTelemetry `SpanExporter`  
• `spanFilter?` – predicate `(span) => boolean` to decide which spans to export. Use the exported helper `isFISpan` to export only FI spans.

### `isFISpan(span)`

Utility predicate that returns `true` if the span already contains FI semantic conventions.


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
- **2026-10-30 — v5/v6 tool-result history:** canonical input-message normalization reads the legacy `result` field and only the first tool content part. Conversations using the current `output` field or multiple results can have incomplete `gen_ai.input.messages`; application tool history is unchanged.
- **2026-10-30 — S5 privacy scope:** credential observation and process-wide echo scrubbing belong to the core agent, including when model coverage suppresses a wire span. This package does not inspect provider state or maintain credential groups/inheritance, and has no extra non-transport credentials to register. Enabling only this adapter or using manual processors without the agent's transport hooks does not discover runtime provider credentials; initialization-seeded FI/environment values are the registry's baseline. Unmarked credentials that never cross an instrumented transport, or application-transformed secrets (encoding, splitting or numeric re-typing), may not be recognized. Register known non-transport values with the core `registerSecret(value)` API; use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS` to withhold sensitive content/error details rather than relying on recognition of transformed values.

## Support

For support, please open an issue in our [GitHub repository](https://github.com/future-agi/traceAI/issues).

