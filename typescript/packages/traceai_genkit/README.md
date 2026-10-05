# @traceai/genkit

Send [Firebase Genkit](https://genkit.dev) (JavaScript) spans to Future AGI. Genkit already creates OpenTelemetry spans for flows, models, tools and steps. `FIGenkitSpanProcessor` is a span processor you pass to Genkit's `enableTelemetry({ spanProcessors })`. It maps Genkit's `genkit:*` attributes to Future AGI span kinds, model and token keys, and exports the mapped copy through `@traceai/fi-core`.

Alpha. Validated with real `genkit` 1.42.0 flows on Genkit's own test model (`mockModel` from `genkit/testing`), exported to a loopback OTLP receiver. No model vendor and no GCP were called.

JavaScript only. Genkit Python and Genkit Go are not covered. This package does not use the Firebase / Google Cloud plugin, cannot run next to it in one process in this release (see [Firebase / Google Cloud telemetry plugin](#firebase--google-cloud-telemetry-plugin)), and does not wrap `ai.generate()`.

## Versions and licence

| Item | Value |
|---|---|
| Tested pin | `genkit` 1.42.0 (`@genkit-ai/core` 1.42.0, `@genkit-ai/ai` 1.42.0) |
| Peer range | `genkit` `^1.42.0` |
| Node | 20.20.2, 22.23.3 and 26.8.1: the full contract suite (every journey) ran once per version with `NODE_BINARY` set to it, and jest ran once per version with that Node first on `PATH`. `TRACEAI_NODE_MATRIX` parametrizes only the CJS/ESM entry-point import check; each of the three runs set it to all three Nodes. See [Tests](#tests). |
| Genkit licence | Apache-2.0 (`node_modules/genkit/LICENSE`, `package.json` `license`) |

`genkit` is a **peer dependency**. Install it yourself. This package never bundles or re-exports Genkit; the pack test hashes every file of the installed `genkit`, `@genkit-ai/core` and `@genkit-ai/ai` and checks that none ships in the tarball.

## Install

```bash
npm install @traceai/genkit @traceai/fi-core genkit
```

```bash
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-genkit-app"
```

fi-core sends the keys as the `X-Api-Key` / `X-Secret-Key` headers. They are never copied onto spans.

## Set up

```typescript
import { genkit } from "genkit"; // import genkit first: it initialises Genkit's telemetry provider
import { enableTelemetry, flushTracing } from "genkit/tracing";
import { register, ProjectType } from "@traceai/fi-core";
import { FIGenkitSpanProcessor, flushOnSignals } from "@traceai/genkit";

const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  projectName: "my-genkit-app", // or FI_PROJECT_NAME; the collector drops a batch with no project_name
  setGlobalTracerProvider: false, // required: Genkit's NodeSDK must own the global provider
});

await enableTelemetry({
  spanProcessors: [new FIGenkitSpanProcessor({ tracerProvider })], // captureContent defaults to false
});

const ai = genkit({ plugins: [/* your model plugins */] });

// Long-running server: flush on SIGTERM / SIGINT before Genkit's own exit handler runs.
flushOnSignals(flushTracing);
```

Three things the setup depends on, each checked in the installed 1.42.0 source:

- `enableTelemetry` and `flushTracing` are exported from `genkit/tracing` (`genkit/src/tracing.ts:43-44`), not from `genkit`. They need Genkit's telemetry provider, which `genkit` / `genkit/beta` initialise at import (`genkit/src/common.ts:173-175`). Calling `enableTelemetry` before anything imported `genkit` throws `FAILED_PRECONDITION: TelemetryProvider is not initialized`.
- `register()` must get `setGlobalTracerProvider: false`. Genkit takes its tracer from the global OpenTelemetry API (`@genkit-ai/core` `src/tracing/instrumentation.ts:96,197`) and its NodeSDK cannot register a global provider once fi-core has. Genkit spans would then skip both this processor and the Dev UI exporter, and reach Future AGI raw (content included, no kind). The processor warns (`TRACEAI_GENKIT_GLOBAL_PROVIDER`) when it detects this.
- `enableTelemetry` keeps Genkit's own telemetry-server processor first and appends yours (`@genkit-ai/core` `src/tracing/node-telemetry-provider.ts:66-77`). The Genkit Dev UI keeps working. Passing `traceExporter` instead of `spanProcessors` throws in Genkit.

Metrics: `enableTelemetry({ spanProcessors })` passes no metric reader, and the NodeSDK Genkit uses (`@opentelemetry/sdk-node` 0.52.1) builds a meter provider only when one is configured, so no Genkit metrics go anywhere. `disableMetrics` and `forceDevExport` are options of the `@genkit-ai/google-cloud` plugin (`src/types.ts:92,110`), not of `enableTelemetry`'s `TelemetryConfig`; TypeScript rejects them there.

### Firebase / Google Cloud telemetry plugin

Using this package together with the Firebase / Google Cloud telemetry plugin (`enableFirebaseTelemetry()` from `@genkit-ai/firebase`, `enableGoogleCloudTelemetry()` from `@genkit-ai/google-cloud`, or `ENABLE_FIREBASE_MONITORING=true`) is **not supported in this release**. Use one or the other per process.

Why: both plugins call the same `enableTelemetry()` (`@genkit-ai/firebase` `src/index.ts:40-44` -> `@genkit-ai/google-cloud` `src/index.ts:38-41`), and every `enableTelemetry()` call replaces Genkit's stored config and starts a second NodeSDK (`@genkit-ai/core` `src/tracing/node-telemetry-provider.ts:64,97-98`). Only the first NodeSDK becomes the global tracer provider, so only the first call's processors get Genkit's spans, while `flushTracing()` flushes only the last call's processors (`node-telemetry-provider.ts:143-151`). Whichever runs second gets no spans, and the first is no longer flushed by `flushTracing()`. Checked locally with genkit 1.42.0 and no plugin: two `enableTelemetry()` calls with recording processors, one flow; the first processor received the flow span and no flush, the second received no span and the flush. `ENABLE_FIREBASE_MONITORING=true` makes Genkit call `enableFirebaseTelemetry()` itself when a span starts before any `enableTelemetry()` call (`@genkit-ai/core` `src/tracing.ts:33,50-62`); the Firebase plugin itself was not run (it needs GCP).

Cost: the Future AGI export adds no Google Cloud cost. The Firebase / Google Cloud plugin exports to Cloud Trace, Logging and Monitoring, which Google bills (Blaze plan), so exporting to both, for example from separate deployments, may cost on GCP for that copy only.

## What you get

### Span kinds

`fi.span.kind` and `gen_ai.span.kind` are both set. The map is written from the type strings in the 1.42.0 source (`genkit:type` labels in `core/src/action.ts:538-543`, `core/src/flow.ts:219-221`, `ai/src/generate/action.ts:147-149`, `ai/src/prompt.ts:273-275,490-492`; action subtypes in `core/src/registry.ts:40-62`).

| `genkit:type` | `genkit:metadata:subtype` | Kind |
|---|---|---|
| `action` | `model`, `background-model` | `LLM` |
| `action` | `tool`, `tool.v2` | `TOOL` |
| `action` | `retriever` | `RETRIEVER` |
| `action` | `embedder` | `EMBEDDING` |
| `action` | `reranker` | `RERANKER` |
| `action` | `evaluator` | `EVALUATOR` |
| `action` | `agent` (beta agents) | `AGENT` |
| `action` | `flow` and every other subtype (`custom`, `prompt`, `executable-prompt`, `util`, `indexer`, `resource`, ...) | `CHAIN` |
| `flowStep` (`ai.run()`), `util` (the `generate` span), `promptTemplate` (`render`), `dotprompt` | n/a | `CHAIN` |
| anything else, or no `genkit:type` | | not set; the span is still exported |

### Attributes

| Future AGI key | Source | Where |
|---|---|---|
| `fi.span.kind`, `gen_ai.span.kind` | table above | every Genkit span |
| `gen_ai.request.model` | `genkit:name` of the model action span (the registered name, e.g. `googleai/gemini-2.5-flash`; not parsed) | model spans |
| `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens` | `usage.inputTokens` / `outputTokens` / `totalTokens` in the model span's `genkit:output` | model spans only |
| `gen_ai.response.finish_reasons` | `finishReason` in the model span's `genkit:output` | model spans |
| `tool.name`, `gen_ai.tool.name` | `genkit:name` of the tool action span | tool spans |
| `session.id` | `genkit:metadata:agent:sessionId` (`ai/src/agent.ts:1061-1063`) | the beta agent span only |
| `session.id`, `user.id`, `metadata`, `tag.tags` | fi-core `setSession` / `setUser` / `setMetadata` / `setTags` around the flow call | every span started in that context |
| `input.value` / `output.value` (+ `application/json` mime types) | `genkit:input` / `genkit:output` | only with `captureContent: true` |
| `genkit:*` | passed through, except the content keys below | every Genkit span |

Unavailable at 1.42.0, and not set: provider, cost, user id (unless the app sets it), retriever documents, and every `GenerationUsage` field the inventory did not see on a span (`thoughtsTokens`, `cachedContentTokens`, the character / image / video / audio counters, `custom`). Those stay inside `genkit:output`.

Tokens are written only on model call spans. The `generate` span's `genkit:output` repeats the last turn's usage; it stays inside that JSON and is not promoted. If any non-model span carries a key the collector promotes (`gen_ai.usage.*`, `llm.token_count.*`, `llm.usage.*`, `gen_ai.cost.*`, `llm.cost.*`; the cost prefixes cover the parts such as `gen_ai.cost.input` and `llm.cost.completion` as well as the totals), the processor moves it to `genkit.usage.<key>`, so the trace-wide token and cost sums equal the model calls.

Errors: Genkit sets the OTel status to `ERROR` and records an `exception` event on every span the error passes through (`core/src/tracing/instrumentation.ts:153-172`). Both are exported; with content off, schema-validation data is cut from them (see [Content](#content)). `genkit:isFailureSource` marks the first failing span.

### Session

Genkit 1.42.0 has no session key on plain flows. Beta agents (`genkit/beta` `defineAgent` / `agent.chat()`) tag the agent span with `genkit:metadata:agent:sessionId`, which becomes `session.id` on that span. For flows, set the session yourself around the call:

```typescript
import { context } from "@opentelemetry/api";
import { setSession } from "@traceai/fi-core";

await context.with(setSession(context.active(), { sessionId: "chat-123" }), () => myFlow(input));
```

## Content

Content is off by default. That is stricter than Genkit, which records inputs and outputs on its spans.

| Genkit key | Default | `captureContent: true` |
|---|---|---|
| `genkit:input`, `genkit:output`, `genkit:init` | dropped | exported, and copied to `input.value` / `output.value` |
| `genkit:metadata:interrupt`, `genkit:metadata:resumed` (tool interrupts) | dropped | exported |
| `genkit:metadata:context` (request context; Genkit redacts `auth` and `secrets`, headers can remain) | dropped | dropped |
| Span status message, `exception.message` and `exception.stacktrace` of `exception` events | cut at `Provided data:`, followed by `[data redacted: captureContent is off]` | exported unchanged |

Usage and finish reason are read from `genkit:output` before it is dropped, so tokens are present with content off.

Genkit's schema-validation error embeds the rejected data in its message: `Schema validation failed. Parse Errors: ... Provided data: <the JSON> Required JSON schema: ...` (`@genkit-ai/core` `src/schema.ts:78`). It is raised, for example, when `ai.generate({ output: { schema } })` gets model output that does not match the schema (the contract `schema` journey), and Genkit copies the message to the status and the exception event of every span the error passes through. With content off, the processor keeps the text before `Provided data:` (status, parse errors and paths), drops the rest (data, schema and, in the stack trace, the stack frames) and appends `[data redacted: captureContent is off]`. `exception.type` and the event itself are kept. Other error messages are exported as thrown: an error your own code throws with user input in its message is not redacted.

The processor never changes Genkit's span. Genkit's Dev UI and any other exporter see the original attributes; Future AGI gets a mapped copy.

fi-core's `TraceConfig` masking (`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and the other `FI_HIDE_*` settings) is not applied to Genkit spans: `captureContent` is the only content switch in this package.

## Flush and shutdown

- `flushTracing()` from `genkit/tracing` (`core/src/tracing.ts:113`) flushes every processor, this one included. Call it before a short script exits.
- `FIGenkitSpanProcessor.forceFlush()` and `shutdown()` never reject. Each is bounded by `flushTimeoutMillis` (default 30000 ms). A collector that is down is logged through the OTel diag logger; the flow still returns.
- `shutdown()` also shuts down the `register()` provider you passed, and Genkit's NodeSDK calls it on SIGTERM (`@genkit-ai/core` `src/tracing/node-telemetry-provider.ts:99,111`), so give Genkit its own `register()` provider instead of sharing one with other traceAI instrumentations.
- SIGTERM: `genkit` installs a SIGTERM / SIGINT listener at import that stops its reflection servers and calls `process.exit(0)` without flushing (`genkit/src/genkit.ts:786-793`). An app listener that awaits `flushTracing()` loses that race. `flushOnSignals(flush)` moves the listeners registered so far behind a bounded `flush` (default 10000 ms) and then runs them in order. Call it after `import "genkit"` and after `await enableTelemetry(...)`. It returns a function that restores the original listeners.
- Call `flushOnSignals` after every other SIGTERM / SIGINT listener is installed: a listener added later is not moved behind the flush and runs at once, in parallel with it, and a second signal (a second Ctrl-C) during the flush is ignored until the flush finishes or its timeout ends.
- Export goes through the fi-core provider's own span processor and exporter. This package adds no queue of its own.

## Span inventory at 1.42.0

`contract/inventory.mjs` runs one flow (an `ai.run()` step, `generateStream` with a tool loop: two model calls, one tool) twice, once called and once streamed, on Genkit's `mockModel`, and records the spans Genkit hands to a processor. 14 spans, all from instrumentation scope `genkit-tracer`, all shaped like `@opentelemetry/sdk-trace-base` 1.25 spans (`parentSpanId`, `instrumentationLibrary`; the processor converts them for fi-core's 2.x exporter).

| Span | `genkit:type` / subtype | Attribute keys (all strings unless noted) |
|---|---|---|
| flow (`inventoryFlow`) | `action` / `flow` | `genkit:type`, `genkit:metadata:subtype`, `genkit:key`, `genkit:name`, `genkit:isRoot` (boolean), `genkit:path`, `genkit:input`, `genkit:output`, `genkit:state`, `genkit:metadata:context` |
| step (`prepare`) | `flowStep` | `genkit:type`, `genkit:name`, `genkit:path`, `genkit:output`, `genkit:state` |
| `generate` | `util` | `genkit:type`, `genkit:name`, `genkit:path`, `genkit:input`, `genkit:output`, `genkit:state` |
| model (`inventory/mock`) | `action` / `model` | `genkit:type`, `genkit:metadata:subtype`, `genkit:key`, `genkit:name`, `genkit:path`, `genkit:input`, `genkit:output`, `genkit:state` |
| tool (`lookup`) | `action` / `tool` | same keys as the model span |

The model span's `genkit:output` is the model response JSON: `message`, `finishReason`, `usage` (`inputTokens`, `outputTokens`, `totalTokens` from the mock), `latencyMs`. The `generate` span's `genkit:output` has `message`, `finishReason`, `usage` (the last turn's), `custom`, `request`. A beta agent run (contract `agent` journey) adds `render` (`promptTemplate`, `genkit:metadata:promptName`), a `runTurn-1` step (`flowStep`, `genkit:metadata:agent:snapshotId`) and the agent span (`action` / `agent`, `genkit:metadata:agent:sessionId`, `genkit:init`). Error spans add `genkit:isFailureSource`.

## Tests

From `typescript/packages/traceai_genkit`:

```bash
pnpm exec jest
```

From the repository root (shared harness in `python/tests/harness`), once per Node version:

```bash
NODE_BINARY=/path/to/node-v20.20.2/bin/node \
TRACEAI_NODE_MATRIX=/path/to/node-v20.20.2/bin/node:/path/to/node-v22.23.3/bin/node:/path/to/node-v26.8.1/bin/node \
PYTHONPATH=python/tests uv run --no-project --python 3.11 --with pytest --with protobuf --with opentelemetry-proto \
  pytest typescript/packages/traceai_genkit/contract -q -p no:cacheprovider --noconftest -o addopts=''
```

The contract suite builds the package, runs `contract/run_fixture.mjs` against the real fi-core exporter and the shared receiver, and checks the collector path and headers, the resource, span names, kinds and parenting, model and tokens, the trace token sum, content off by default with an opt-in control run, one closed model span for a streamed flow, error status and exception events, schema-validation data cut from status and exception events with content off (and kept after opt-in), SIGTERM flushing (with a control that shows a plain listener loses the race), a collector that is down, Genkit's Dev UI reflection server and telemetry export in `GENKIT_ENV=dev`, the inventory drift check, the packed tarball, and the CJS / ESM entry points. `NODE_BINARY` is the node every fixture journey and the inventory check run on (the build and `pnpm pack` steps use the `node` on `PATH`). `TRACEAI_NODE_MATRIX` (path-separated) parametrizes only the CJS / ESM entry-point import check, which loads the built package through a symlink in a temporary `node_modules` (not a clean `npm install`); unset, it is `NODE_BINARY` alone. The suite ran on 20.20.2, 22.23.3 and 26.8.1, one run each with the matrix above: 22 passed per run (19 tests plus the import check on three Nodes). jest ran on the same three Nodes (that Node first on `PATH`): 68 passed each.
