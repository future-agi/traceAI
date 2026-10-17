# @traceai/voltagent

Future AGI tracing for [VoltAgent](https://github.com/VoltAgent/voltagent). VoltAgent already
emits OpenTelemetry spans, but with its own keys (`entity.type`, `span.type`, `ai.model.name`,
`llm.usage.prompt_tokens`, `conversation.id`, bare `input` / `output`). `FIVoltAgentSpanProcessor`
copies those onto the keys the Future AGI collector and trace UI read (`fi.span.kind`,
`gen_ai.*`, `session.id`) and exports the copy through the traceAI exporter.

It plugs into VoltAgent's own `ObservabilityConfig.spanProcessors`. It does not patch `Agent`, and
it does not change the span VoltAgent hands to its other processors (VoltOps, local storage,
websocket): it exports a mapped copy.

Status: alpha. Tested against `@voltagent/core` 2.11.0 (the latest release) with the AI SDK mock
model, and end to end into a local Future AGI collector with live models: `api.openai.com`
(`gpt-4.1-nano`) and an OpenAI-compatible endpoint, both through `@ai-sdk/openai`.

## Install

```bash
npm install @traceai/voltagent @traceai/fi-core @voltagent/core
```

`@voltagent/core` is a peer dependency (`^2.11.0`); this package never bundles it.

```bash
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-voltagent-app"
```

## Set up

```typescript
import { ProjectType, register } from "@traceai/fi-core";
import { FIVoltAgentSpanProcessor } from "@traceai/voltagent";
import { Agent, VoltAgent, VoltAgentObservability } from "@voltagent/core";

const tracerProvider = register({
  projectName: "my-voltagent-app",
  projectType: ProjectType.OBSERVE,
  // Required: VoltAgent registers its own provider globally. If the Future AGI provider is
  // global, VoltAgent's spans bypass every ObservabilityConfig processor (this one included).
  setGlobalTracerProvider: false,
});

const observability = new VoltAgentObservability({
  spanProcessors: [
    // ...keep any processors you already have here...
    new FIVoltAgentSpanProcessor({ tracerProvider }),
  ],
});

const agent = new Agent({ name: "assistant", instructions: "...", model: /* AI SDK model */ });
new VoltAgent({ agents: { assistant: agent }, observability });
```

The constructor warns (`console.warn`) when the provider you pass is the global one.

VoltAgent's own VoltOps export, local storage and websocket processors keep running; you do not
remove them. VoltAgent's sampling wrapper only wraps its VoltOps exporter, so a processor in
`spanProcessors` receives every span. VoltAgent's span filter (instrumentation scope
`@voltagent/core` by default) applies to every processor in the array, including this one.

### Options

| Option | Default | Meaning |
|---|---|---|
| `tracerProvider` | | The provider from `register()`. Spans carry its resource (`project_name`, `project_type`) and go out through its OTLP exporter (`/tracer/v1/traces`, `x-api-key` / `x-secret-key`). |
| `exporter`, `resource` | | Instead of `tracerProvider`: any `SpanExporter`, and the resource to export with. |
| `traceConfig` | content captured | fi-core `TraceConfigOptions`, the same flags as every traceAI instrumentor: `hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`, `hideInputText`, `hideOutputText`. Each falls back to its `FI_HIDE_*` env var, then to the fi-core default (`false`). See Privacy. |
| `batch` | `true` | Batch through a `BatchSpanProcessor` (with `tracerProvider`, it wraps the provider's exporter). `false` exports each span as it ends. |
| `batchConfig` | SDK defaults | `BatchSpanProcessor` settings: queue 2048, batch 512, delay 5000 ms. Spans beyond a full queue are dropped by the SDK. |
| `usageReconciliationTimeoutMs` | `30000` | How long an llm span waits for its operation's root span (see Tokens). |

## What is mapped

Every original VoltAgent key stays on the exported copy, except content you hide (see Privacy),
credentials, stack traces, and promoted token keys on spans that are not model calls (moved under
`voltagent.`). Kept strings have local paths rewritten (see Privacy).

| Future AGI field | VoltAgent source (2.11.0) | Notes |
|---|---|---|
| `fi.span.kind`, `gen_ai.span.kind`, `openinference.span.kind` | `span.type`, `entity.type` | root agent span (`entity.type=agent`, no `span.type`) and `span.type=agent` → `AGENT`; `llm` → `LLM`; `tool` → `TOOL`; `retriever`, `vector` → `RETRIEVER`; `embedding` → `EMBEDDING`; `memory` with `memory.operation=read` → `RETRIEVER`, other memory operations (`write`, `write_steps`) → `CHAIN`; `guardrail`, `middleware`, `summary`, workflow and unknown types → `CHAIN`. An existing `fi.span.kind` is kept. |
| `gen_ai.operation.name` | kind | `invoke_agent`, `chat`, `execute_tool`, `embeddings` |
| `session.id`, `gen_ai.conversation.id` | `conversation.id` | not overwritten if already set |
| `user.id` | `user.id` | already the Future AGI key |
| `gen_ai.request.model`, `gen_ai.response.model` | `llm.model` (llm spans), `ai.model.name` (agent span) | the model name VoltAgent resolved |
| `gen_ai.provider.name` | `llm.provider`, `ai.model.provider`; else the agent's AI SDK model object's `provider` | VoltAgent parses `llm.provider` from a model string (text before `/`); it is not a vendor id. For a model object VoltAgent records only `modelId`, so the processor looks the agent up by `entity.id` in VoltAgent's agent registry (filled by `new VoltAgent({ agents })`) and maps the object's `provider` id: `openai.*` → `openai`, `anthropic.*` → `anthropic`, `google.generative-ai` → `google`, `google.vertex.*` / `vertex.*` → `vertexai`, `azure.*` → `azure`, `amazon-bedrock` → `aws`, `mistral.*` → `mistralai`, and `groq`, `xai`, `deepseek`, `cohere`, `cerebras`, `togetherai` (→ `together`), `fireworks`, `ollama`, `huggingface`. Other ids (an OpenAI-compatible provider's own name, the AI Gateway) stay unset. Future AGI prices a call from `gen_ai.request.model` and its tokens, not from the provider |
| `gen_ai.request.temperature`, `.max_tokens`, `.top_p` | `llm.temperature`, `llm.max_output_tokens`, `llm.top_p`, `ai.model.*` | |
| `gen_ai.response.finish_reasons` | `llm.finish_reason`, `ai.response.finish_reason` | |
| `gen_ai.usage.input_tokens`, `.output_tokens`, `.total_tokens` | `llm.usage.prompt_tokens`, `.completion_tokens`, `.total_tokens` | **llm spans only** (see Tokens) |
| `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_read_tokens` | `llm.usage.cached_tokens` | llm spans only, when > 0 |
| `gen_ai.usage.reasoning.output_tokens`, `gen_ai.usage.output_tokens.reasoning` | `llm.usage.reasoning_tokens` | llm spans only, when > 0 |
| `voltagent.usage.input_tokens`, `.output_tokens`, `.total_tokens`, `.cache_read_tokens`, `.reasoning_tokens` | `usage.prompt_tokens`, ... on the agent span | the operation total; namespaced so it is not counted twice |
| `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.description` | `tool.name`, `tool.call.id`, `tool.description` | |
| status, `exception` events, `error.*` | passed through, filtered | a failing tool is an `ERROR` span. `exception` events keep `exception.type` and `exception.message`; `exception.stacktrace` and `error.stack` are never exported. The status message, `error.message` and `exception.message` have local paths, emails and this machine's user and host names removed; with inputs or outputs hidden they are dropped and an `ERROR` status carries the error type (`VoltAgentError`, `AI_APICallError`, ...) |
| `input.value`, `output.value` | `input`, `output` (`vector.query` / `embedding.query` as retriever input). llm spans: `llm.messages` (the last 10 messages VoltAgent sent) as input; as output, the agent span's `output` when the operation has exactly one successful `llm:generateText` / `llm:streamText` span (VoltAgent records no response text on llm spans) | by default; not with `hideInputs` / `hideOutputs` (see Privacy) |
| cost | not mapped | VoltAgent sets `usage.cost` only from OpenRouter provider metadata; it is passed through unchanged and not promoted |

### Tokens

The Future AGI collector promotes `gen_ai.usage.*`, `llm.usage.*` and `llm.token_count.*` into
token columns, and `gen_ai.cost.total` / `llm.cost.total` (or, without a total, the parts
`gen_ai.cost.input` + `.output`, `llm.cost.prompt` + `.completion`) into its cost column, on any
span; Observe sums them over the whole trace. So promoted keys appear only on llm (model-call)
spans; on any other span they are moved under `voltagent.` (for example
`voltagent.gen_ai.cost.total`). The agent span's summed usage is exported as `voltagent.usage.*`.

VoltAgent 2.11.0 wraps a whole multi-step AI SDK call (for example: tool call, then answer) in one
`llm:<operation>` span and records the AI SDK's `usage` on it, which is the **last step only**,
while the agent span gets `totalUsage` across all steps. To keep the trace total equal to the
tokens of every model step, the processor holds each llm span until its operation's root span ends.
When the operation has exactly one successful `llm:generateText` or `llm:streamText` span and the
root total is larger, the exported copy of that llm span carries the root total
(`voltagent.usage.reconciled = true`) and keeps the last-step values under
`voltagent.llm.last_step_usage.*`. Otherwise (several main calls, no root within
`usageReconciliationTimeoutMs`, or a flush in between), the llm span is exported as VoltAgent wrote
it. Failed attempts (`ERROR` llm spans) are never reconciled.

#### Limits

A trace's promoted token total can be lower than what the model calls used. It is never counted
twice.

- `generateObject` / `streamObject` (deprecated in 2.11.0) open no llm span. Their usage appears only
  as `voltagent.usage.*` on the agent span, so the collector's token columns show 0 for them.
- Tool routing with a provider tool: VoltAgent runs the provider tool through an internal
  `generateText` call (`executeProviderToolViaCallTool` → `runInternalGenerateText`), which opens a
  second successful `llm:generateText` span in the same operation. With two main calls the
  processor does not reconcile, so the main multi-step span keeps its last-step usage and the
  earlier steps are missing from the total. The internal call's usage is on its own span, is not
  part of the agent span's total, and is counted once.
- A flush between an llm span's end and its root's end (`forceFlush`, a serverless flush on finish,
  another request's flush), or a root that does not end within `usageReconciliationTimeoutMs`,
  releases the llm span unreconciled: it carries last-step usage only.

## Privacy

Content follows the traceAI `TraceConfig` convention, like every traceAI instrumentor: it is
**exported by default**, and the standard flags hide it. VoltAgent writes the prompt, the answer,
system instructions, message history, tool arguments and results, memory payloads, conversation
summaries and plans onto its spans; the Future AGI copy carries them unless you hide them.

```bash
export FI_HIDE_INPUTS=true    # prompts, instructions, messages, tool arguments, queries, memory reads
export FI_HIDE_OUTPUTS=true   # answers, tool results, summaries, plans, streamed guardrail chunks
```

or in code (an option wins over its env var; only the string `true` hides, as in fi-core):

```typescript
new FIVoltAgentSpanProcessor({ tracerProvider, traceConfig: { hideInputs: true, hideOutputs: true } });
```

| Flag (env var) | What it hides on the exported copy |
|---|---|
| `hideInputs` (`FI_HIDE_INPUTS`) | input-side keys: `input`, `input.value`, `agent.instructions`, `agent.context`, `agent.messages`, `agent.messages.ui`, `llm.messages`, `workflow.context`, `workspace.sandbox.command` / `.args`, `workspace.fs.path` / `.pattern`, `workspace.sandbox.cwd`, `planagent.task.description`, `resume.data`, any key with an `input` segment (`middleware.input.original`, `eval.input`, ...) and string keys ending in `messages`, `instructions`, `query`, `context`, `prompt(s)`, `arguments`, `args` (`tool.search.query`, `vector.query`, ...) |
| `hideOutputs` (`FI_HIDE_OUTPUTS`) | output-side keys: `output`, `output.value`, `agent.summary.preview` / `.text`, `planagent.todos`, `planagent.task.response_preview`, `guardrail.chunk.text`, any key with an `output` segment (`guardrail.output.after`, `eval.output`, ...) and string keys ending in `completion` |
| either of the two | keys that hold both sides: `agent.stateSnapshot`, `workflow.stateSnapshot` (step source and the run input), `suspension.checkpoint`, `suspension.data`, `agent.workingMemory.finalContent`, `eval.scorer.metadata` (the run's input and output), `eval.expected`, `guardrail.result.metadata`, string keys ending in `data`, `checkpoint`, `content`; and run-time free text: the status message, `error.message`, `exception.message`, `eval.scorer.error_message`, `workspace.error`, `guardrail.message`, `guardrail.reason`, `suspension.reason`, `cancellation.reason` and the workflow step reasons |
| `hideInputMessages` / `hideOutputMessages` | the message lists only (`llm.messages`, `agent.messages`, `agent.messages.ui`; `gen_ai.output.messages`) |
| `hideInputText` / `hideOutputText` | keeps the message lists' roles and replaces each message's `content` / `parts` with `__REDACTED__` |

Span events are filtered the same way: the `guardrail.stream.process` event's `guardrail.chunk.text`,
`workflow.suspended`'s `suspension.data` / `suspension.checkpoint` and `workflow.resumed`'s
`resume.data` follow the flags; event names, times and structural attributes
(`guardrail.chunk.index`, `resume.step_index`, ...) always stay. Counts such as `llm.messages.count`
are never content.

Never exported, in either mode:

- **Credentials**: keys with a segment like `api_key`, `secret`, `secret_key`, `password`,
  `authorization`, `cookie` or `header(s)`, on span and event attributes. The filter is by key: a
  JSON value such as `llm.provider_options` is exported whole, so do not put credentials in
  provider options.
- **Stack traces**: `error.stack` and `exception.stacktrace` are dropped, and V8 frames
  (`    at ...`) and Python tracebacks are cut from every exported string, including stack traces
  inside JSON (a failed tool's error object in `llm.messages`).
- **Local paths**: in every exported string (JSON documents included) the working directory becomes
  `[cwd]`, the home directory `~`, and other absolute local paths, `file://` URLs and Windows paths
  `[path]`. URL paths such as `/v1/chat/completions` are left alone.
- **In error text** (status message, `error.message`, `exception.message` and the other free-text
  keys above): emails become `<email>` and this machine's user and host names `<redacted>`.
  Captured message content is exported as the model saw it: an email in a prompt or a tool result
  stays unless you hide inputs/outputs.

VoltAgent itself has no capture flag; a VoltOps export of the same run still has the original keys.
`FI_PII_REDACTION` is not applied by this processor.

## Flushing, serverless and failures

- Spans are batched. Flush before a serverless handler returns:
  `await observability.forceFlush()` (VoltAgent flushes every processor) or
  `await processor.forceFlush()`. VoltAgent ends some memory spans in the background just after
  `generateText` resolves, so flush at the end of the handler.
- A one-off script does not exit after `forceFlush()`: `new VoltAgent()` keeps the process running
  (it installs signal handlers for a graceful shutdown). End it with
  `await observability.shutdown()`, which flushes every processor too.
- On platforms with `waitUntil`, VoltAgent's serverless observability can flush in the background:
  `setWaitUntil(waitUntil: (promise: Promise<unknown>) => void)` from `@voltagent/core`
  (`src/observability/wait-until.ts`).
- `register()` attaches a `SimpleSpanProcessor`, which sends one request per span; the OTLP
  exporter rejects more than 30 requests in flight, so a burst loses spans. With `tracerProvider`,
  this processor batches through the provider's exporter instead (on OpenTelemetry SDK 2.x,
  `register({ batch: true })` does not replace that processor). The provider's exporter is shared:
  `shutdown()` flushes it but never shuts it down.
- A collector that is down or rejects a batch never fails the agent: export errors are logged
  through the OpenTelemetry diag logger, and `forceFlush()` / `shutdown()` never throw.

## Tested versions

| Component | Version |
|---|---|
| `@voltagent/core` | 2.11.0 (npm `latest` on 2026-10-04 and still on 2026-10-16; `next` is 3.0.0-next.1, not tested) |
| `ai` | 6.0.97 (mock model, `ai/test` `MockLanguageModelV3`, in the tests); 6.0.309 with `@ai-sdk/openai` 3.0.130 (end to end) |
| `@traceai/fi-core` exporter | workspace build in the tests; 1.0.0 from npm end to end (OpenTelemetry SDK 2.x, OTLP HTTP exporter 0.202.0) |
| Node.js | 20.20.2, 22.18.0 and 26.8.1: jest (20.20.2, 26.8.1) and the contract tool journey, span-event journey and ESM/CJS entry-point check (the tests `TRACEAI_NODE_MATRIX` parametrizes); every other contract test and the end-to-end run on 26.8.1 |
| Models (end to end) | `gpt-4.1-nano` on `api.openai.com`; `gpt-5.6-terra` through an OpenAI-compatible endpoint |

`@voltagent/core` 2.11.0 is MIT-licensed (`license` in its `package.json`; the published tarball
has no LICENSE file, its README and `docs/community/licence.md` carry the MIT text, "Copyright (c)
2025 VoltAgent"). It is a peer dependency and is not redistributed here.

## Not covered

- Workflows (`entity.type=workflow`) map to `CHAIN`. The tests run one workflow that suspends and
  resumes, to check its span events and the root's kind; workflow step spans are not asserted.
- Retriever, embedding and vector spans are mapped from source reading; the fixture runs only the
  agent's memory spans (`memory.read`, `memory.write`, `memory.steps.write`).
- VoltAgent logs (`RemoteLogProcessor`) are not exported. Eval scorer spans are exported with
  their ids, status, score and threshold; `eval.scorer.metadata`, `eval.expected`, `eval.input`,
  `eval.output` and `eval.scorer.error_message` follow the hide flags (see Privacy).
- Tokens are on the llm spans, not on the agent root (see Tokens), so the Observe trace list's
  Tokens column, which reads the root span, is empty for VoltAgent traces; the trace view shows the
  tokens and cost of each model call.
- Only the Node runtime was tested; edge runtimes were not.

## Development

```bash
pnpm --filter @traceai/voltagent run build
pnpm --filter @traceai/voltagent test
# Shared-harness contract test (from the repo root). Every test runs on NODE_BINARY (default: node
# on PATH); with TRACEAI_NODE_MATRIX=/path/node20:/path/node22:..., the tool journey, the span-event
# journey and the ESM/CJS import check run on each listed binary instead.
PYTHONPATH="python/tests" uv run --no-project --python 3.11 --with pytest --with protobuf \
  --with opentelemetry-proto pytest typescript/packages/traceai_voltagent/contract -q \
  -p no:cacheprovider --noconftest -o addopts=''
```
