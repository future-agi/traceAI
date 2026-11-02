# @traceai/genkit

Send [Firebase Genkit](https://genkit.dev) (JavaScript) spans to Future AGI. Genkit already creates OpenTelemetry spans for flows, models, tools and steps. `FIGenkitSpanProcessor` is a span processor you pass to Genkit's `enableTelemetry({ spanProcessors })`. It maps Genkit's `genkit:*` attributes to Future AGI span kinds, model and token keys, and exports the mapped copy through `@traceai/fi-core`.

Requires Node.js 20 or later and Genkit `^1.42.0`. The processor maps native spans without adding a second trace around your model calls.

JavaScript only. Genkit Python and Genkit Go are not covered. This package does not use the Firebase / Google Cloud plugin, cannot run next to it in one process in this release (see [Firebase / Google Cloud telemetry plugin](#firebase--google-cloud-telemetry-plugin)), and does not wrap `ai.generate()`.

`genkit` is an Apache-2.0 licensed **peer dependency**. Install it alongside this package; it is not bundled or re-exported.

## Install

```bash
npm install @traceai/genkit @traceai/fi-core genkit
```

```bash
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-genkit-app"
```

fi-core sends traces to `https://api.futureagi.com/tracer/v1/traces` with the keys in the `X-Api-Key` / `X-Secret-Key` headers. They are never copied onto spans.

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
  spanProcessors: [new FIGenkitSpanProcessor({ tracerProvider })], // content on by default; FI_HIDE_INPUTS / FI_HIDE_OUTPUTS hide it
});

const ai = genkit({ plugins: [] }); // add your model plugins here

// Long-running server: flush on SIGTERM / SIGINT before Genkit's own exit handler runs.
flushOnSignals(flushTracing);

const welcome = ai.defineFlow("welcome", async ({ name }) => {
  return ai.run("greet", async () => `Hello, ${name}!`);
});

console.log(await welcome({ name: "Ada" }));
await flushTracing(); // flush before a short script exits
```

Open the `my-genkit-app` project in Future AGI to see the `welcome` flow and its child `greet` step. Your model and tool calls inside flows are traced through the same native Genkit telemetry.

Configure telemetry once, before the first flow, model or tool runs:

- Import `genkit` before calling `enableTelemetry`. Both `enableTelemetry` and `flushTracing` come from `genkit/tracing`.
- Pass `setGlobalTracerProvider: false` to `register()`. Genkit must own the global OpenTelemetry provider; otherwise native spans bypass this processor's mapping and privacy rules. The processor warns with `TRACEAI_GENKIT_GLOBAL_PROVIDER` when it detects that configuration.
- Pass the processor through `spanProcessors`, not `traceExporter`. Genkit manages its own Dev UI telemetry separately; do not depend on a particular processor ordering.

The processor exports traces, not metrics. Do not pass the Firebase plugin's `disableMetrics` or `forceDevExport` options to `enableTelemetry`; they are not part of that API.

Genkit's Dev UI continues to receive its own telemetry. In Genkit 1.43 and later, the Dev UI uses independent trace and span IDs. Future AGI preserves the native OpenTelemetry IDs and parent relationships; do not use Dev UI IDs to look up those traces.

### Firebase / Google Cloud telemetry plugin

Using this package together with the Firebase / Google Cloud telemetry plugin (`enableFirebaseTelemetry()` from `@genkit-ai/firebase`, `enableGoogleCloudTelemetry()` from `@genkit-ai/google-cloud`, or `ENABLE_FIREBASE_MONITORING=true`) is **not supported in this release**. Use one or the other per process.

Both integrations configure Genkit's global OpenTelemetry provider. Calling `enableTelemetry()` more than once can leave spans with the first provider while `flushTracing()` flushes only the last configuration. Configure telemetry once, using one integration per process.

Cost: the Future AGI export adds no Google Cloud cost. The Firebase / Google Cloud plugin exports to Cloud Trace, Logging and Monitoring, which Google bills (Blaze plan), so exporting to both, for example from separate deployments, may cost on GCP for that copy only.

## What you get

### Span kinds

`fi.span.kind` and `gen_ai.span.kind` are both set from Genkit's native span type and action subtype.

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
| `gen_ai.request.model`, `gen_ai.response.model` | `genkit:name` of the model action span without its plugin namespace: `googleai/gemini-3.5-flash` becomes `gemini-3.5-flash`, the name Future AGI's price table uses. The full registered name stays on `genkit:name`. Genkit reports no resolved model version, so both keys hold the requested model | model spans |
| `gen_ai.provider.name` | the plugin namespace: `googleai` → `google`, `vertexai` → `vertexai`, `openai` → `openai`, `anthropic` → `anthropic`, `deepseek` → `deepseek`, `xai` → `xai`, `ollama` → `ollama`, `mistral` → `mistralai`, `groq` → `groq`, `aws-bedrock` → `aws`, `azure-openai` → `azure`. Any other namespace: not set | model spans |
| `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens` | `usage.inputTokens` / `outputTokens` / `totalTokens` in the model span's `genkit:output` | model spans only |
| `gen_ai.usage.output_tokens.reasoning` / `gen_ai.usage.cache_read_tokens` | `usage.thoughtsTokens` / `cachedContentTokens`, when the model plugin reports them (the Google AI / Vertex AI plugin does; Gemini's `outputTokens` excludes thinking tokens, which are in `totalTokens`) | model spans only |
| `gen_ai.response.finish_reasons` | `finishReason` in the model span's `genkit:output` | model spans |
| `tool.name`, `gen_ai.tool.name` | `genkit:name` of the tool action span | tool spans |
| `session.id` | `genkit:metadata:agent:sessionId`  | the beta agent span only |
| `session.id`, `user.id`, `metadata`, `tag.tags` | fi-core `setSession` / `setUser` / `setMetadata` / `setTags` around the flow call | every span started in that context |
| `embedding.model_name`, `gen_ai.provider.name` | `genkit:name` of the embedder action span without its plugin namespace (`googleai/gemini-embedding-001` becomes `gemini-embedding-001`), provider as for models | embedder spans |
| `input.value` / `output.value` (+ `application/json` mime types) | `genkit:input` / `genkit:output` | by default; `__REDACTED__` (no mime type) when hidden, see [Content](#content) |
| `genkit:*` | passed through, except the content keys below | every Genkit span |

Cost is not set by this package: fi-collector prices each model span from its tokens and `gen_ai.request.model` (an exact lookup in its litellm price table), so a model the table lists, such as `gemini-3.5-flash`, gets a cost and one it does not list shows none. Not mapped: user id (unless the app sets it), retriever documents, and the other `GenerationUsage` fields (the character / image / video / audio counters, `custom`). Those stay inside `genkit:output`.

Tokens are written only on model call spans. The `generate` span's `genkit:output` repeats the last turn's usage; it stays inside that JSON and is not promoted. If any non-model span carries a key the collector promotes (`gen_ai.usage.*`, `llm.token_count.*`, `llm.usage.*`, `gen_ai.cost.*`, `llm.cost.*`; the cost prefixes cover the parts such as `gen_ai.cost.input` and `llm.cost.completion` as well as the totals), the processor moves it to `genkit.usage.<key>`, so the trace-wide token and cost sums equal the model calls.

Errors: Genkit sets the OTel status to `ERROR` and records an `exception` event on every span the error passes through. Both are exported, without `exception.stacktrace` and without local paths, emails, or this machine's user and host names in the message; with error details hidden the message is omitted (see [Content](#content)). `genkit:isFailureSource` marks the first failing span.

### Session

Plain flows have no native session key. Beta agents (`genkit/beta` `defineAgent` / `agent.chat()`) tag the agent span with `genkit:metadata:agent:sessionId`, which becomes `session.id` on that span. For flows, set the session yourself around the call:

```typescript
import { context } from "@opentelemetry/api";
import { setSession } from "@traceai/fi-core";

await context.with(setSession(context.active(), { sessionId: "chat-123" }), () => myFlow(input));
```

## Content

Content follows the traceAI `TraceConfig` convention, the same as every other traceAI instrumentor: Genkit's inputs and outputs (flow input and output, prompts and model responses, tool arguments and results, retriever queries and documents, embedder input and vectors) are **exported by default**, and the standard switches hide them. Set them as environment variables, or pass the same options as `traceConfig`; the option wins over the variable, and only the string `true` (any case) hides.

```bash
export FI_HIDE_INPUTS=true    # every span: genkit:input, genkit:init, genkit:metadata:resumed
export FI_HIDE_OUTPUTS=true   # every span: genkit:output, genkit:metadata:interrupt
```

```typescript
new FIGenkitSpanProcessor({ tracerProvider, traceConfig: { hideInputs: true, hideOutputs: true } });
```

| Switch (env / `traceConfig`) | Effect on the exported copy |
|---|---|
| `FI_HIDE_INPUTS` / `hideInputs` | Input-side keys dropped, `input.value` = `__REDACTED__`, no `input.mime_type`, and the error message rule below |
| `FI_HIDE_OUTPUTS` / `hideOutputs` | Output-side keys dropped, `output.value` = `__REDACTED__`, no `output.mime_type`, and the error message rule below |
| `FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_OUTPUT_MESSAGES` | The same, on model actions and their surrounding `generate` spans, including the registered `/util/generate` action |
| `FI_HIDE_INPUT_TEXT` / `FI_HIDE_OUTPUT_TEXT` | On those spans, every `text` and `reasoning` part becomes `__REDACTED__`; roles, tool requests, finish reason and usage stay |
| `FI_HIDE_INPUT_IMAGES` / `hideInputImages` | Replace image media in native model requests and embedded request copies with `__REDACTED__`; retain text and non-image media |
| `FI_HIDE_EMBEDDING_VECTORS` | On embedder spans, every vector becomes `__REDACTED__` |
| `FI_HIDE_ERROR_DETAILS` / `hideErrorDetails` | Omit status descriptions and error detail attributes on spans and events. When unset, enabled by any input/output/message/text hide flag |

A generation response can include a copy of its request. `hideInputs` or `hideInputMessages` removes that embedded request from both `genkit:output` and `output.value`, without removing the model's reply. `hideInputText` redacts the embedded request's text and reasoning instead; `hideInputImages` redacts its image media. These rules also apply to background-model operations and their check/cancel spans. If a native response cannot be parsed, the processor redacts it when field-level masking is required.

Hidden or not, tokens, finish reason, model, provider, kinds, names and session are exported: they are read from Genkit's span before anything is hidden.

Never exported, in either mode:

| What | How |
|---|---|
| `genkit:metadata:context` (request context; Genkit redacts `auth` and `secrets`, headers can remain) | dropped |
| `exception.stacktrace` of `exception` events | dropped |
| Stack frames and local file paths in any exported string (content included) | frames removed; the working directory becomes `[cwd]`, the home directory `~`, other absolute local paths, `file://` URLs and Windows paths `[path]`. URL paths and Genkit action paths (`genkit:path`) are left alone |
| Emails and this machine's user and host names in the span status message and `exception.message` | replaced by `<email>` / `<redacted>` |

Error messages can quote prompts, tool arguments, or rejected structured output. Set `FI_HIDE_ERROR_DETAILS=true` or `traceConfig: { hideErrorDetails: true }` to omit status descriptions, `exception.message`, `exception.stacktrace`, `error.message`, and namespaced attributes ending in `.exception.message`, `.exception.stacktrace`, `.error.message`, or `.error_message`. This applies to span attributes and all event attributes. In native generation responses and background operations (including polling/cancellation), it also removes `finishMessage`, `error.message`, and `error.details`, retaining error status and numeric code, finish reason, operation ID, and usage. The status code, exception event, error type, timestamps, and other metadata remain. Your app still receives the full error.

When `hideErrorDetails` is unset, any of `hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`, `hideInputText`, or `hideOutputText` enables it, including their environment-variable equivalents. `hideInputImages` or `hideEmbeddingVectors` alone does not. An explicit `hideErrorDetails: false` or `FI_HIDE_ERROR_DETAILS=false` opts out, even when content is hidden, so error messages may expose that content. Options take precedence over environment variables. For `FI_HIDE_ERROR_DETAILS`, `true` and `false` are case-insensitive and whitespace is trimmed; an empty or invalid value falls back to derivation from the content flags.

The processor never changes Genkit's span. Genkit's Dev UI and any other exporter see the original attributes; Future AGI gets a mapped copy.

The `captureContent` option of earlier pre-release builds is removed. Passing it logs a warning; `captureContent: false` still hides inputs and outputs (unless `traceConfig` or the `FI_HIDE_*` variables say otherwise), so an old configuration never exports more than it asked for.

## Flush and shutdown

- `flushTracing()` from `genkit/tracing` flushes every processor, this one included. Call it before a short script exits.
- `FIGenkitSpanProcessor.forceFlush()` and `shutdown()` never reject. Each is bounded by `flushTimeoutMillis` (default 30000 ms). A collector that is down is logged through the OTel diag logger; the flow still returns.
- `shutdown()` also shuts down the `register()` provider you passed, and Genkit's NodeSDK calls it on SIGTERM, so give Genkit its own `register()` provider instead of sharing one with other traceAI instrumentations.
- SIGTERM: `genkit` installs a SIGTERM / SIGINT listener at import that stops its reflection servers and calls `process.exit(0)` without flushing. An app listener that awaits `flushTracing()` loses that race. `flushOnSignals(flush)` moves the listeners registered so far behind a bounded `flush` (default 10000 ms) and then runs them in order. Call it after `import "genkit"` and after `await enableTelemetry(...)`. It returns a function that restores the original listeners.
- Call `flushOnSignals` after every other SIGTERM / SIGINT listener is installed: a listener added later is not moved behind the flush and runs at once, in parallel with it, and a second signal (a second Ctrl-C) during the flush is ignored until the flush finishes or its timeout ends.
- Export goes through the fi-core provider's own span processor and exporter. This package adds no queue of its own.

## Troubleshooting

- **No traces:** check `FI_API_KEY`, `FI_SECRET_KEY`, and the project name. Pass `setGlobalTracerProvider: false` to `register()` and configure Genkit telemetry only once.
- **Traces disappear on exit:** call `await flushTracing()` before a script exits. For servers, install `flushOnSignals(flushTracing)` after other signal handlers.
- **Content is still visible in the Dev UI:** privacy options apply only to the mapped Future AGI export. Genkit's Dev UI and other exporters keep their original content.
