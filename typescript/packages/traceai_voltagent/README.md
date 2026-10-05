# @traceai/voltagent

Future AGI tracing for [VoltAgent](https://github.com/VoltAgent/voltagent). VoltAgent already
emits OpenTelemetry spans, but with its own keys (`entity.type`, `span.type`, `ai.model.name`,
`llm.usage.prompt_tokens`, `conversation.id`, bare `input` / `output`). `FIVoltAgentSpanProcessor`
copies those onto the keys the Future AGI collector and trace UI read (`fi.span.kind`,
`gen_ai.*`, `session.id`) and exports the copy through the traceAI exporter.

It plugs into VoltAgent's own `ObservabilityConfig.spanProcessors`. It does not patch `Agent`, and
it does not change the span VoltAgent hands to its other processors (VoltOps, local storage,
websocket): it exports a mapped copy.

Status: alpha. Fixture-validated against `@voltagent/core` 2.11.0 with the AI SDK mock model; no
live model provider was called.

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
    new FIVoltAgentSpanProcessor({ tracerProvider }), // captureContent defaults to false
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
| `captureContent` | `false` | Export prompts, messages, instructions, tool arguments/results and retrieval queries. |
| `batch` | `true` | Batch through a `BatchSpanProcessor` (with `tracerProvider`, it wraps the provider's exporter). `false` exports each span as it ends. |
| `batchConfig` | SDK defaults | `BatchSpanProcessor` settings: queue 2048, batch 512, delay 5000 ms. Spans beyond a full queue are dropped by the SDK. |
| `usageReconciliationTimeoutMs` | `30000` | How long an llm span waits for its operation's root span (see Tokens). |

## What is mapped

Every original VoltAgent key stays on the exported copy, except content (below), credentials, and
promoted token keys on spans that are not model calls (moved under `voltagent.`).

| Future AGI field | VoltAgent source (2.11.0) | Notes |
|---|---|---|
| `fi.span.kind`, `gen_ai.span.kind`, `openinference.span.kind` | `span.type`, `entity.type` | root agent span (`entity.type=agent`, no `span.type`) and `span.type=agent` → `AGENT`; `llm` → `LLM`; `tool` → `TOOL`; `retriever`, `vector` → `RETRIEVER`; `embedding` → `EMBEDDING`; `memory` with `memory.operation=read` → `RETRIEVER`, other memory operations (`write`, `write_steps`) → `CHAIN`; `guardrail`, `middleware`, `summary`, workflow and unknown types → `CHAIN`. An existing `fi.span.kind` is kept. |
| `gen_ai.operation.name` | kind | `invoke_agent`, `chat`, `execute_tool`, `embeddings` |
| `session.id`, `gen_ai.conversation.id` | `conversation.id` | not overwritten if already set |
| `user.id` | `user.id` | already the Future AGI key |
| `gen_ai.request.model`, `gen_ai.response.model` | `llm.model` (llm spans), `ai.model.name` (agent span) | the model name VoltAgent resolved |
| `gen_ai.provider.name` | `llm.provider`, `ai.model.provider` | VoltAgent parses it from the model string (text before `/`); it is not a vendor id |
| `gen_ai.request.temperature`, `.max_tokens`, `.top_p` | `llm.temperature`, `llm.max_output_tokens`, `llm.top_p`, `ai.model.*` | |
| `gen_ai.response.finish_reasons` | `llm.finish_reason`, `ai.response.finish_reason` | |
| `gen_ai.usage.input_tokens`, `.output_tokens`, `.total_tokens` | `llm.usage.prompt_tokens`, `.completion_tokens`, `.total_tokens` | **llm spans only** (see Tokens) |
| `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_read_tokens` | `llm.usage.cached_tokens` | llm spans only, when > 0 |
| `gen_ai.usage.reasoning.output_tokens`, `gen_ai.usage.output_tokens.reasoning` | `llm.usage.reasoning_tokens` | llm spans only, when > 0 |
| `voltagent.usage.input_tokens`, `.output_tokens`, `.total_tokens`, `.cache_read_tokens`, `.reasoning_tokens` | `usage.prompt_tokens`, ... on the agent span | the operation total; namespaced so it is not counted twice |
| `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.description` | `tool.name`, `tool.call.id`, `tool.description` | |
| status, `exception` events, `error.*` | passed through | a failing tool is an `ERROR` span; `exception` events keep `exception.type`, `.message` and `.stacktrace`. Other event attributes are filtered like span attributes (see Content) |
| `input.value`, `output.value` | `input`, `output` (`vector.query` / `embedding.query` as retriever input) | **only with `captureContent: true`** |
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
When the operation has exactly one successful `generateText` / `streamText` / `generateObject` /
`streamObject` llm span and the root total is larger, the exported copy of that llm span carries
the root total (`voltagent.usage.reconciled = true`) and keeps the last-step values under
`voltagent.llm.last_step_usage.*`. Otherwise (several main calls, failed attempts, no root within
`usageReconciliationTimeoutMs`, or a flush in between), the llm span is exported as VoltAgent wrote it.

## Content

Off by default. VoltAgent itself has no capture flag: it writes the prompt, the answer, system
instructions, message history, tool arguments and results, memory payloads, conversation summaries
and plans onto its spans. The Future AGI copy drops them unless you pass `captureContent: true`.
This is stricter than VoltAgent; a VoltOps export of the same run still has the originals.

Dropped keys: `input`, `output`, `agent.instructions`, `agent.messages`, `agent.messages.ui`,
`agent.context`, `agent.stateSnapshot`, `llm.messages`, `workflow.context`,
`workflow.stateSnapshot` (step source and the run input), `workspace.sandbox.command`,
`workspace.sandbox.args`, `suspension.checkpoint`, `agent.summary.preview`, `agent.summary.text`
(summarization), `agent.workingMemory.finalContent` (working memory), `planagent.todos`,
`planagent.task.description`, `planagent.task.response_preview` (PlanAgent),
`guardrail.chunk.text`, `suspension.data`, `resume.data` (span events, below), any key with an
`input` or `output` segment (`middleware.input.original`, `guardrail.output.after`, ...), and string
values whose last segment is `messages`, `instructions`, `query`, `context`, `data`, `checkpoint`,
`prompt(s)`, `completion`, `content`, `arguments` or `args` (`tool.search.query`, `vector.query`,
`workflow.resume.data`, ...). Counts such as `llm.messages.count` stay.

Span events are filtered the same way. VoltAgent puts content in event attributes too: an output
guardrail with a streaming handler adds a `guardrail.stream.process` event per answer chunk with
the chunk in `guardrail.chunk.text`, and a workflow adds `workflow.suspended` (`suspension.data`,
`suspension.checkpoint`) and `workflow.resumed` (`resume.data`). Without `captureContent`, those
keys are removed from the exported events; the event names, times and other attributes
(`guardrail.chunk.index`, `suspension.reason`, `resume.step_index`, ...) stay. `exception` events
keep `exception.type`, `exception.message` and `exception.stacktrace`, which contain whatever text
the thrown error carries.

Credentials are never exported, whatever `captureContent` says: keys with a segment like
`api_key`, `secret`, `secret_key`, `password`, `authorization`, `cookie` or `header(s)` are dropped,
from span attributes and from event attributes.

## Flushing, serverless and failures

- Spans are batched. Flush before a short-lived process or a serverless handler returns:
  `await observability.forceFlush()` (VoltAgent flushes every processor) or
  `await processor.forceFlush()`. `observability.shutdown()` flushes too. VoltAgent ends some
  memory spans in the background just after `generateText` resolves, so flush at the end of the
  handler.
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
| `@voltagent/core` | 2.11.0 (npm `latest` on 2026-10-04) |
| `ai` (mock model, `ai/test` `MockLanguageModelV3`) | 6.0.97 |
| `@traceai/fi-core` exporter | workspace (OpenTelemetry SDK 2.0.1, OTLP HTTP exporter 0.202.0) |
| Node.js | 20.20.2, 22.23.3, 26.8.1 |

`@voltagent/core` 2.11.0 is MIT-licensed (`license` in its `package.json`; the published tarball
has no LICENSE file, its README and `docs/community/licence.md` carry the MIT text, "Copyright (c)
2025 VoltAgent"). It is a peer dependency and is not redistributed here.

## Not covered

- Workflows (`entity.type=workflow`) map to `CHAIN`; they were not exercised by the fixture.
- Retriever, embedding and vector spans are mapped from source reading; the fixture runs only the
  agent's memory spans (`memory.read`, `memory.write`, `memory.steps.write`).
- VoltAgent logs (`RemoteLogProcessor`) and evals are not exported.
- Only the Node runtime was tested; edge runtimes were not.

## Development

```bash
pnpm --filter @traceai/voltagent run build
pnpm --filter @traceai/voltagent test
# Shared-harness contract test (from the repo root):
PYTHONPATH="python/tests" uv run --no-project --python 3.11 --with pytest --with protobuf \
  --with opentelemetry-proto pytest typescript/packages/traceai_voltagent/contract -q \
  -p no:cacheprovider --noconftest -o addopts=''
```
