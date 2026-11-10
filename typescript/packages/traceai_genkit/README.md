# @traceai/genkit

Send [Firebase Genkit](https://genkit.dev) (JavaScript) spans to Future AGI. Genkit already creates OpenTelemetry spans for flows, models, tools and steps. The exported zero-argument `enable()` hook lets `@traceai/auto` map those spans in its existing Future AGI export branch, without application wrappers, a second exporter, or changes to the application's telemetry configuration. The manual `FIGenkitSpanProcessor` API remains available for applications that configure Genkit's `enableTelemetry({ spanProcessors })` themselves.

Requires Node.js 20 or later and Genkit `^1.42.0`. The processor maps native spans without adding a second trace around your model calls.

JavaScript only. Genkit Python and Genkit Go are not covered. This package does not use the Firebase / Google Cloud plugin and does not wrap `ai.generate()`. The manual setup below must not initialize a second Genkit telemetry SDK alongside the Firebase / Google Cloud plugin (see [Firebase / Google Cloud telemetry plugin](#firebase--google-cloud-telemetry-plugin)). The automatic hook never initializes Genkit telemetry; cloud-plugin coexistence has not been tested against GCP.

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

## Automatic activation hook

`@traceai/genkit` exports `enable(): EnableGenkitResult` for the `@traceai/auto` bootstrap to call **after** it installs its Future AGI export processor. No application code needs to call Genkit's `enableTelemetry()` or construct `FIGenkitSpanProcessor` for this path. Importing this package alone has no activation side effect.

- The zero-argument hook discovers the initialized agent through its versioned `traceai.auto.agent.v1` process state and decorates **only** that agent's export processor. It never decorates the application's aggregate processor or replaces the global tracer provider. Native OTel 1.x and 2.x Genkit spans are supported.
- Mapping occurs on a separate span snapshot before the existing agent's authoritative export-time sanitizer. Span and parent IDs, start/end times, duration, scope and the application's resource survive. The original span remains available to application exporters and the Genkit Dev UI unchanged.
- For automatic exports, content is carried only in the canonical `input.value` / `output.value` families. Native `genkit:input`, `genkit:output`, `genkit:init`, resumed and interrupt payload aliases are removed from the FI snapshot so hide flags cannot be bypassed by a duplicate alias. Only the framework `generate` utility (`genkit:type=util`, `genkit:name=generate`) loses its output's duplicated `request`; flow/tool result fields named `request` are preserved. If that utility's JSON output is truncated or malformed, the whole output is redacted because its duplicated input cannot safely be separated. The manual API's native-attribute behavior is unchanged.
- Privacy is owned by the core agent's bounded, process-wide known-secret registry (S5), not by Genkit ancestry or request-local credentials. The core transport hooks observe outgoing credentials even when a model span suppresses the wire span. The final core sanitizer removes every registered value from every exported span, including earlier-ending children, background work and unrelated application spans. This integration performs no credential discovery and holds no completion buffer. Native and FI exporters receive ended spans immediately.
- At span start, native Genkit model/embedder/reranker actions provide a non-recording coverage marker **only to the agent's ancestry tracker**. That suppresses duplicate wire capture even across intermediate native SDK spans, without changing the application's active context, parentage or span attributes. Framework/tool spans alone do not suppress wire capture.
- The agent's existing sanitizer and exporter own privacy, export, deadlines, flushing and shutdown; Genkit does not wrap `forceFlush()` or `shutdown()`. Registry/sanitizer failure is handled by the core fail-closed boundary, preserving identity and timing without content. No signal listener, exit hook, network call or Genkit import is added. Automatic error diagnostics delegate `hideErrorDetails`, including explicit `false`, to the owning sanitizer.
- Model and framework `generate` inputs separate their top-level `config` into `gen_ai.request.parameters` instead of duplicating it inside `input.value`. `FI_HIDE_LLM_INVOCATION_PARAMETERS=true` (or `traceConfig.hideLLMInvocationParameters`) suppresses that governed family without hiding the prompt. Option values override the environment. Native telemetry and manual mapping are unchanged.
- Repeated calls, including separate CJS/ESM copies, keep the first processor and configuration. Disable-after-enable and reconfiguration are unsupported; restart the process to change them. Missing credentials and `OTEL_SDK_DISABLED=true` prevent activation. A missing agent returns `manual`; unsupported/frozen processors return `failed`. Internal failures do not reach the application, and a mapping failure never forwards a raw, possibly hidden span.

For agent embedders, `enable({ spanProcessor, traceConfig })` accepts an explicit **FI-only export branch** instead of discovering the agent. This is not an API for passing an application provider or a shared aggregate processor. Its `onStart` must accept a readable snapshot, and its export path must run the core process-wide sanitizer. The hook re-associates snapshots with the original parent context for mapping metadata, not credential inheritance. A plugin integration that directly sees a config-only credential outside a transport can call `registerSecret(value)` from `@traceai/fi-core` before executing work. Genkit's mapper does not inspect plugin objects or invent its own credential-name rules.

This package supplies the activation entry point; dispatch from the launcher's integration catalog belongs to `@traceai/auto` and is not changed here. Do not combine this hook with a manually attached `FIGenkitSpanProcessor` for the same FI destination.

## Manual set up

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

Using the **manual setup** above together with the Firebase / Google Cloud telemetry plugin (`enableFirebaseTelemetry()` from `@genkit-ai/firebase`, `enableGoogleCloudTelemetry()` from `@genkit-ai/google-cloud`, or `ENABLE_FIREBASE_MONITORING=true`) is **not supported**. Use one Genkit telemetry initialization per process. The automatic hook does not call `enableTelemetry()` or replace any configured exporter; this avoids the second-initialization problem, but actual cloud-plugin export has not been reverified with that hook.

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
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` / `hideLLMInvocationParameters` | Automatic path: omit generation `config` from `gen_ai.request.parameters`; it is never duplicated inside generation `input.value`. Does not change the separate manual mapper |

A generation response can include a copy of its request. `hideInputs` or `hideInputMessages` removes that embedded request from both `genkit:output` and `output.value`, without removing the model's reply. `hideInputText` redacts the embedded request's text and reasoning instead; `hideInputImages` redacts its image media. These rules also apply to background-model operations and their check/cancel spans. If a native response cannot be parsed, the processor redacts it when field-level masking is required.

The manual processor also applies fi-core's semantic content rules to attributes from `setAttributes` and `setPromptTemplate`, including opaque `input.value`/`output.value`, raw payloads, prompt-template variables, and semantic message copies. Native structured generation envelopes retain their field-level masking.

Background `Operation.metadata` is opaque provider state and may repeat requests, responses, images, vectors, or errors. Its exported copy becomes `__REDACTED__` whenever any input/output/message/text/image/vector or error-detail hiding setting applies, including nested generation responses and check/cancel inputs and outputs. This deliberately hides innocuous metadata too. Operation identifiers, completion state, and permitted response fields remain available; application objects and Genkit's own telemetry are unchanged. With all these settings off, metadata and semantic content are exported as before.

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
- Manual export goes through the fi-core provider's own span processor and exporter without another queue. Automatic mapping also exports immediately, without its own queue; the owning agent remains responsible for delivery.

## Troubleshooting

- **No traces:** check `FI_API_KEY`, `FI_SECRET_KEY`, and the project name. Pass `setGlobalTracerProvider: false` to `register()` and configure Genkit telemetry only once.
- **Traces disappear on exit:** call `await flushTracing()` before a script exits. For servers, install `flushOnSignals(flushTracing)` after other signal handlers.
- **Content is still visible in the Dev UI:** privacy options apply only to the mapped Future AGI export. Genkit's Dev UI and other exporters keep their original content.

## Native span inventory

`contract/inventory.mjs` runs one flow (an `ai.run()` step, `generateStream` with a tool loop: two model calls, one tool) twice, once called and once streamed, on Genkit's `mockModel`, and records the spans Genkit hands to a processor. 14 spans, all from instrumentation scope `genkit-tracer`, all shaped like `@opentelemetry/sdk-trace-base` 1.25 spans (`parentSpanId`, `instrumentationLibrary`; the processor converts them for fi-core's 2.x exporter).

| Span | `genkit:type` / subtype | Attribute keys (all strings unless noted) |
|---|---|---|
| flow (`inventoryFlow`) | `action` / `flow` | `genkit:type`, `genkit:metadata:subtype`, `genkit:key`, `genkit:name`, `genkit:isRoot` (boolean), `genkit:path`, `genkit:input`, `genkit:output`, `genkit:state`, `genkit:metadata:context` |
| step (`prepare`) | `flowStep` | `genkit:type`, `genkit:name`, `genkit:path`, `genkit:output`, `genkit:state` |
| `generate` | `util` | `genkit:type`, `genkit:name`, `genkit:path`, `genkit:input`, `genkit:output`, `genkit:state` |
| model (`inventory/mock`) | `action` / `model` | `genkit:type`, `genkit:metadata:subtype`, `genkit:key`, `genkit:name`, `genkit:path`, `genkit:input`, `genkit:output`, `genkit:state` |
| tool (`lookup`) | `action` / `tool` | same keys as the model span |

The model span's `genkit:output` is the model response JSON: `message`, `finishReason`, `usage` (`inputTokens`, `outputTokens`, `totalTokens` from the mock), `latencyMs`. The `generate` span's `genkit:output` has `message`, `finishReason`, `usage` (the last turn's), `custom`, `request`. A beta agent run (contract `agent` journey) adds `render` (`promptTemplate`, `genkit:metadata:promptName`), a `runTurn-1` step (`flowStep`, `genkit:metadata:agent:snapshotId`) and the agent span (`action` / `agent`, `genkit:metadata:agent:sessionId`, `genkit:init`). Error spans add `genkit:isFailureSource`.

## Shared-core S6 release gate (2026-10-30)

**BLOCKING — zero-code agent, B2/B5.** The current `@traceai/auto` core has not adopted the shared S6 privacy catalog. Its exact-name credential lists miss the catalog's `x-vault-token` transport header and nested `database_password` field; its key/length media heuristic misses short PNG-magic base64 under `content`. These are one shared-core adoption defect, not three catalog gaps, application-transformed-secret limitations, or a reason to restore Genkit credential discovery. The fix must unify core transport observation and export-time recognition using the shared catalog; this package continues to delegate both to the core.

`contract/auto_s5.cjs` includes three failing release regressions: `catalog-header` uses real instrumented fetch beneath an intermediate native SDK span while the Genkit model's wire span is suppressed, with an immediately exported application child's ordinary-key attribute array, status and event; `catalog-field` and `catalog-media` compare real flow `output.value` exactly against `pattern-password-response` and `magic-png-standard-response_body` in the shared golden bundle. The header case also reads its request from `pattern-token-headers`. No local predicate, copied fixture expectation, manual registration or mocked sanitizer masks the shared defect. Both source and built Genkit mappings currently fail all three privacy assertions while application results, native exports, span IDs and timing remain intact. Fixing this gate requires edits in the shared core's `src/wire/privacy.ts` and its export-time catalog integration, outside this package; it cannot be corrected by restoring forbidden integration-local discovery. This release gate is separate from the OTHER limitations below.

## Known limitations

- **2026-10-30 — Bootstrap contract.** `enable()` requires the agent's existing FI-only export branch (versioned process-state contract `traceai.auto.agent.v1`), or the explicit `spanProcessor` option. It does not create an agent or activate itself on import. This package change alone does not enable dispatch in the separate `@traceai/auto` catalog.
- **2026-10-30 — Host ancestry contract.** Wire de-duplication requires the owning agent to track the parent context passed to its FI processor's `onStart` (the current `@traceai/auto` does). The hook supplies covered non-recording markers to that tracker; older/custom hosts with no ancestry tracking cannot consume them. Tests exercise the real wire hook with a fake local fetch transport, a real Genkit model and an intermediate native SDK span; no external provider endpoint is contacted.
- **2026-10-30 — Native payload aliases.** Automatic FI snapshots omit Genkit-specific input/output, agent-init/resume and interrupt aliases rather than exporting a second content copy outside canonical privacy families. Specialized agent-resume/interrupt payloads are therefore not independently displayed by the automatic path; native application telemetry and the manual processor are unaffected.
- **2026-10-30 — Lifecycle and cloud validation.** Automatic activation adds no lifecycle hooks and relies on the owning agent for bounded normal-exit/SIGTERM flushing and export-time privacy. The new tests exercise real Genkit 1.42.0 with a local mock model and OTel 1.x/2.x providers; they do not re-run GCP/Firebase export, collector transport, Node-version matrices or process-signal journeys. Historical contract-suite results below refer to the manual setup, not these automatic-path checks.
- **2026-10-30 — Late activation metadata.** Spans that started before activation still use the current process-wide registry when they end. An active Genkit callback can recover its session/user context; a detached pre-activation span ending outside that callback may lack those context attributes. No credential ancestry recovery or root buffering is attempted. Activate before calls start for full context fidelity.
- **2026-10-30 — Application-extracted signed secrets (ROUND4 final #1, OTHER; zero-code agent).** A signature extracted from a signed URL into ordinary output text, when it never crossed an instrumented transport and was not registered, cannot be identified from that plain text. The original URL's signed query fields are still structurally protected wherever captured. Use `FI_HIDE_OUTPUTS=true` / `FI_HIDE_OUTPUT_TEXT=true` (and `FI_HIDE_INPUTS=true` / `FI_HIDE_INPUT_TEXT=true` for sensitive inputs), or register a known config credential with the core `registerSecret(value)` before use. The `signed-url` and `context-url` regressions pin the limitation and verify the hide controls.
- **2026-10-30 — Application JSON-encoded credentials (ROUND4 final #7, OTHER; zero-code agent).** An application may JSON-encode a credential object inside a string, parse it itself and return an unmarked echo. If no transport observed the value and no integration registered it, the echo is not guaranteed to be recognized. Use `FI_HIDE_OUTPUTS=true` / `FI_HIDE_OUTPUT_TEXT=true` and the corresponding input controls, or register the original known string credential before use. The `json-string` regression verifies unchanged application output and hide-control mitigation.
- **2026-10-30 — Application numeric credential re-typing (ROUND4 final #8, OTHER; zero-code agent).** A numeric value re-typed by application code into an ordinary output string is not a known string credential unless an instrumented transport observed it or an integration registered the original string. Use `FI_HIDE_OUTPUTS=true` / `FI_HIDE_OUTPUT_TEXT=true` and the corresponding input controls. The `numeric` regression pins this limitation and verifies the hide controls. Credential-named captured fields remain governed by the core sanitizer.
- **2026-10-30 — Transport/registration boundary.** Credentials that never cross an instrumented transport and carry no credential marker in the captured data may be exported as ordinary text; a later registry observation cannot retroactively change an already exported span. Config-only credentials should be registered before work starts. Manual instrumentors and custom hosts without the agent's transport hooks have only their FI/environment seeds and explicitly registered values. Use the `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT` and `FI_HIDE_OUTPUT_TEXT` controls when application data may contain unknown secrets.
- **2026-10-30 — Invocation config separation.** Automatic generation inputs no longer repeat their top-level `config`; it is available only as `gen_ai.request.parameters` when invocation capture is enabled. This applies to identified model and framework `generate` spans, not arbitrary flow/tool input fields named `config`. The manual mapper retains its historical native-input behavior.
- **2026-10-30 — Request-field accuracy (review finding corrected).** Earlier automatic mapping removed legitimate flow/tool return fields named `request`. Removal is now restricted to the identified framework `generate` utility; real flow/tool regression tests preserve those fields. Its duplicated request is still intentionally omitted from FI output, and custom native spans imitating that framework utility's labels receive the same treatment.
- **2026-10-30 — Error-detail override (review finding corrected).** Earlier automatic mapping applied the manual processor's stricter error policy and overrode `hideErrorDetails=false`. The automatic branch now delegates that choice to the owning sanitizer, with a real-flow error-identity/diagnostics regression test. The manual API resolves the tri-state error policy documented above; the owning fi-core exporter may independently apply its own privacy configuration.

## Semantic attribute privacy

The manual `FIGenkitSpanProcessor` and automatic `enable()` adapter both use an **exact metadata allowlist**, not a list of known sensitive leaves. Apart from those model/provider/system identifiers, IDs, token usage, finish reasons, timings, counts and status/provenance fields, attributes in `gen_ai.*`, `llm.*`, input/output, tool/agent, message, audio, embedding and retrieval families are content. Dot- and bracket-flattened descendants are content too; a metadata key does not exempt its descendants. On both paths, the same content policy runs on span attributes, context-merged attributes, event attributes, link attributes and exported resource attributes. The manual path uses the FI provider's resource; the automatic path masks a detached view of the native span's resource. Automatic status and exception-detail hiding still belongs to the owning exporter, preserving its explicit `hideErrorDetails=false` override.

Vector query predicates (`db.vector.query.filter`), prompts (including `gen_ai.prompts`), tool definitions/schemas/descriptions, invocation parameters and agent inputs follow the input flags. Responses, tool results and agent outputs follow the output flags. Unknown families and unqualified messages/audio transcripts can contain either side, so **either side's input/output/message/text hiding flag masks them**. In particular an audio transcript without a trustworthy per-surface direction is not assumed to be public under input-only hiding. Opaque values use `__REDACTED__` (MIME companions may be omitted). Canonical `gen_ai.input.messages` and `gen_ai.output.messages` retain fi-core's structured text/image redaction; native Genkit envelopes retain their documented field-level handling. Supported legacy `traceai.attribute_projection.metadata` JSON attribute-map capsules recursively use that same policy, including nested capsules. The final FI boundary uses the shared side policy for Genkit exports instead of broader tool/request family rules; a value-bound internal receipt preserves field-masked native aliases without bypassing credential, media, PII or loss checks. Application spans/resources are never changed.

## Tests

`contract/content_allowlist_round5.mjs` derives its key inventory from the repository's semantic-convention exports and literal attribute keys in Genkit/fi-core source, composes the actual message/tool/embedding/document collection paths from those constants, then adds flattened descendants and explicit future-family probes. It checks each key's default behavior, both side policies and message/text variants on all five export surfaces through both the manual processor and the automatic adapter using real Genkit, a synthetic loopback provider and a complete OTLP receiver. The automatic cases initialize the real source `@traceai/auto` agent against an application-owned OTel provider, call the public `enable()` adapter and use the real FI wire exporter; no exporter or processor is substituted. Default-export controls require the marker to remain visible; raw audio payload keys already governed by fi-core's always-on media policy instead require its exact omission placeholder even with content hiding off. It records per-attribute outcomes and checks metadata retention, trace parenting and native-object immutability.

The automatic-path tests use real `genkit` 1.42.0 and `genkit/testing` mock models. `auto-genkit.test.ts` covers tool loops, streaming/no-agent parity, errors, kinds/tokens/parenting, untouched native spans and hide flags on OTel 1.x; `auto-existing-provider.test.ts` covers OTel 2.x coexistence and wire de-duplication through intermediate native spans. `auto.test.ts` covers idempotence, session context, the real core sanitizer, immediate child/background/unrelated-span export, unchanged flush/shutdown methods, registry failure, and transactional fail-open attachment. `auto-privacy.test.ts` keeps truncated generation-output, invocation controls, real-flow/error identity and session activation regressions while using the core registration API for its synthetic config-only credentials; these unit tests do not claim transport observation. The actual zero-code transport tests are in `contract/auto_s5.cjs`. Jest is serial and keeps its cache under this package's `node_modules`.

`contract/auto_s5.cjs` loads the real source `@traceai/auto` **register bootstrap**, not a call to `init()` or `enable()` in the application. Only its recording-exporter factory is substituted; the real registry, sanitizer, Genkit and fetch transport remain active. It covers the ROUND4 final #2–#6 ancestry/race/suppression shapes with credentials sent through synthetic loopback authorization headers before their echoes (the S5 B2 boundary). It asserts immediate child export, subsequent unrelated-span scrubbing, no duplicate wire spans, application response helpers/results, one FI export per native span, and unchanged IDs/timing. Ordinary-key arrays, output strings, status, event names and event attributes are checked for exact replacements, with the original values retained on the native export. `late-context` puts both observed credentials on the same still-open child and ends it after its root; `transport` checks suppression and immediate child export beneath an intermediate native SDK span. The `failed-context` mode also exhausts the real core transport-body discovery budget and checks fail-closed identity/timing-only exports, without replacing the registry. Both built CJS and source mapping are tested. #1, #7 and #8 pin application-transformed/unobserved secrets as OTHER and separately verify hiding the output. These tests never contact a provider or collector service.

From `typescript/packages/traceai_genkit`:

```bash
pnpm test # jest --runInBand
# After compiling CJS + ESM output:
node contract/auto_entrypoints.mjs
```

Repository bootstrap smoke test, from `typescript` after building this package (requires the workspace's `ts-node` development dependency):

```bash
TS_NODE_PROJECT=packages/traceai_auto/tsconfig.json \
  node -r ts-node/register/transpile-only packages/traceai_genkit/contract/auto_bootstrap.cjs
```

`contract/auto_privacy.cjs` runs all ten cumulative mapping/lifecycle regressions against the **actual current agent source**, the built Genkit integration, real Genkit and an in-process recording exporter. S5 credential cases first send their synthetic key through a loopback transport; they no longer rely on deleted end-of-root discovery. Run each mode in a fresh process from `typescript` with the same `TS_NODE_PROJECT` / `ts-node` setup:

```bash
for mode in truncated masked-secret context-secret inflight request-field error-override tail-secret context-tail-secret child-secret invocation; do
  TS_NODE_PROJECT=packages/traceai_auto/tsconfig.json \
    node -r ts-node/register/transpile-only packages/traceai_genkit/contract/auto_privacy.cjs "$mode" || exit
done
```

Run the zero-code S5 matrix from `typescript` (each Node invocation must be individually wrapped by the shared-host helper when using the shared laptop):

```bash
for source in 0 1; do
  for mode in signed-url context-url fi-wrapper late-context background failed-context transport json-string numeric; do
    TS_NODE_PROJECT=packages/traceai_auto/tsconfig.json VERIFY_SOURCE="$source" \
      bash /Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh \
      node -r ts-node/register/transpile-only packages/traceai_genkit/contract/auto_s5.cjs "$mode" || exit
  done
  for mode in signed-url context-url json-string numeric; do
    TS_NODE_PROJECT=packages/traceai_auto/tsconfig.json VERIFY_SOURCE="$source" VERIFY_HIDE=1 \
      bash /Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh \
      node -r ts-node/register/transpile-only packages/traceai_genkit/contract/auto_s5.cjs "$mode" || exit
  done
done
```

Run the S6 shared-core release gate separately from the S5 regression matrix (from `typescript`). Each assertion must pass after the core adopts the catalog; these are not expected-failure tests:

```bash
for source in 0 1; do
  for mode in catalog-header catalog-field catalog-media; do
    TS_NODE_PROJECT=packages/traceai_auto/tsconfig.json VERIFY_SOURCE="$source" \
      bash /Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh \
      node -r ts-node/register/transpile-only packages/traceai_genkit/contract/auto_s5.cjs "$mode" || exit
  done
done
```

S6-gate revalidation on **2026-10-30** passed **9 Jest suites / 141 tests**, all **26 strengthened S5 source/built matrix cases**, **10 cumulative modes**, **7 built-entry assertions**, **14 bootstrap assertions**, and **6 independent zero-code hand-checks** of prior findings #2, #3 and #6 (three shapes repeated against source and built mapping; exact array/status/event scrubbing, immediate exports and unchanged application/native IDs/timing). The S6 gate still has **0 passed / 6 failed**: one shared-core B2/B5 blocker with three variants across two mappings. Existing builds were exercised without rebuilding because only the contract harness and documentation changed in this follow-up. The out-of-scope core defect is not fixed or downgraded to a limitation. All three dated application-transformation limitations and their hide-control mitigations were rechecked; none is a new limitation.

Earlier S5 shared-host validation on **2026-10-30** passed the package build, **9 Jest suites / 141 tests**, **26 zero-code source/built matrix cases** (including eight hide-control mitigations), **4 additional strengthened failure/wrapper cases**, **10 cumulative mapping/lifecycle modes**, **7 built CJS/ESM entry assertions** and **14 source-bootstrap assertions**, with zero failures. Every build, suite and Node invocation was individually wrapped in `heavy.sh`. The failure cases exercise actual transport discovery exhaustion, not only a simulated registry error; unit tests additionally check longest-first scrubbing across arrays, links, scope, resource and tracestate, snapshot immutability and non-finite attributes. No provider or remote collector was contacted.

Historical ROUND4 validation (before the S5 simplification) also passed 9 Jest suites / 141 tests, 10 cumulative modes, 7 built-entry assertions and 14 bootstrap assertions. S5 removes tests of deleted credential-group/buffer mechanics and replaces them with immediate export and core-registry regressions. This does not claim external cloud or signal-transport revalidation.

From the repository root (shared harness in `python/tests/harness`), once per Node version:

```bash
NODE_BINARY=/path/to/node-v20.20.2/bin/node \
TRACEAI_NODE_MATRIX=/path/to/node-v20.20.2/bin/node:/path/to/node-v22.23.3/bin/node:/path/to/node-v26.8.1/bin/node \
PYTHONPATH=python/tests uv run --no-project --python 3.11 --with pytest --with protobuf --with opentelemetry-proto \
  pytest typescript/packages/traceai_genkit/contract -q -p no:cacheprovider --noconftest -o addopts=''
```

The contract suite builds the package, runs `contract/run_fixture.mjs` against the real fi-core exporter and the shared receiver, and checks the collector path and headers, the resource, span names, kinds and parenting, model and tokens, the trace token sum, content exported by default and hidden with `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` (tokens, kinds and model kept), no stack trace, local path, user or host name in any journey, one closed model span for a streamed flow, error status and exception events (the message replaced by the error type when content is hidden), schema-validation data absent from status and exception events with content hidden (and kept by default), SIGTERM flushing (with a control that shows a plain listener loses the race), a collector that is down, Genkit's Dev UI reflection server and telemetry export in `GENKIT_ENV=dev`, the inventory drift check, the packed tarball, and the CJS / ESM entry points. `NODE_BINARY` is the node every fixture journey and the inventory check run on (the build and `pnpm pack` steps use the `node` on `PATH`). `TRACEAI_NODE_MATRIX` (path-separated) parametrizes only the CJS / ESM entry-point import check, which loads the built package through a symlink in a temporary `node_modules` (not a clean `npm install`); unset, it is `NODE_BINARY` alone. Re-verification 2026-10-16 (TraceConfig privacy, embedder mapping): the contract suite ran once with `NODE_BINARY` 20.20.2 and once with 22.18.0, `TRACEAI_NODE_MATRIX` set to both: 22 passed per run (20 tests plus the import check on two Nodes). jest ran on the same two Nodes: 95 passed each. The earlier review rounds ran the suite on 20.20.2, 22.23.3 and 26.8.1.
