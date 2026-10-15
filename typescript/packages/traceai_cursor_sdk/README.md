# @traceai/cursor-sdk

OpenTelemetry tracing for the [Cursor TypeScript SDK](https://cursor.com/docs/sdk/typescript) (`@cursor/sdk`). Each `agent.send()` becomes one trace in Future AGI: an AGENT span for the run, with child spans for tool calls, thinking and assistant text, the model, token usage and the run status. Every run of one agent shares a session (the agent id), so a multi-turn conversation reads as one session.

Tested with `@cursor/sdk` 1.0.37 (Node 22.13+), local runtime. This package traces the SDK inside your Node process. It is not the Cursor IDE hook integration, which traces the Cursor editor through `.cursor/hooks.json`.

## Install

```bash
npm install @traceai/cursor-sdk @traceai/fi-core @cursor/sdk
```

`@cursor/sdk` is a peer dependency. Nothing in this package imports it at runtime.

## Use

```ts
import * as CursorSDK from "@cursor/sdk";
import { register, ProjectType } from "@traceai/fi-core";
import { CursorSDKInstrumentation, shutdown } from "@traceai/cursor-sdk";

const tracerProvider = register({ projectName: "cursor-sdk-app", projectType: ProjectType.OBSERVE });
const instrumentation = new CursorSDKInstrumentation({ tracerProvider });
instrumentation.manuallyInstrument(CursorSDK);

const agent = await CursorSDK.Agent.create({
  apiKey: process.env.CURSOR_API_KEY!,
  model: { id: "auto" },
  local: { cwd: process.cwd() },
});
const run = await agent.send("List the files in this folder and read README.md");
for await (const event of run.stream()) {
  // your own handling; events are passed through unchanged
}
await run.wait();
await shutdown(tracerProvider);
```

`register()` reads `FI_API_KEY`, `FI_SECRET_KEY` and `FI_BASE_URL`. `@cursor/sdk` is ESM first, so pass the module namespace to `manuallyInstrument()`. A CommonJS app can instead call `registerInstrumentations({ instrumentations: [new CursorSDKInstrumentation()] })` before `require("@cursor/sdk")`.

`Agent.prompt()` is traced too: it calls `Agent.create()`, `send()` and `wait()` internally. `instrumentation.wrapAgent(agent)` traces an agent you got some other way.

### What the wrapper does and does not do

- It patches the static `Agent.create` and `Agent.resume`, and `send`, `stream` and `wait` on the agent and run objects they return. You get the SDK's own objects back, and `run.stream()` yields the SDK's own events unchanged.
- It never reads the run's event stream on its own. The local runtime's stream is a single-consumer queue, so a second reader would steal your events. Tool, thinking and assistant spans exist only for events your code reads through `run.stream()`.
- Tracing failures are logged with `diag.debug` and never reach your code.

## Spans

```
Cursor Agent run              AGENT  session.id = agent id, model, tokens, status
├─ Cursor thinking            CHAIN  one per thinking block (deltas joined), cursor.thinking.duration_ms
├─ Cursor assistant message   LLM    one per run of consecutive text deltas
└─ Cursor tool: <name>        TOOL   one per tool_call call_id: running -> completed | error
```

| Attribute | Where | Source |
| --- | --- | --- |
| `session.id`, `gen_ai.conversation.id`, `cursor.agent.id` | all | `agent.agentId` (`agent-<uuid>` local, `bc-<uuid>` cloud) |
| `cursor.runtime` | run | `local` or `cloud`, from the agent id |
| `cursor.run.id`, `cursor.request.id` | run | `run.id`, `request` event / `run.requestId` |
| `gen_ai.request.model`, `cursor.model.params` | run, assistant | `send({ model })`, `agent.model`, `run.model`, `RunResult.model` |
| `cursor.tools` | run | `system` event, when the runtime sends one |
| `gen_ai.usage.*` | run | see Token usage |
| `cursor.run.status`, `cursor.run.last_status_event`, `cursor.run.duration_ms` | run | `RunResult` / run handle / `status` events |
| `tool.name`, `gen_ai.tool.call.id`, `cursor.tool.status` | tool | `tool_call` events |
| `cursor.tool.args_truncated`, `cursor.tool.result_truncated` | tool | the SDK's `truncated` flags |

Status:

- Run `finished`: OK. Run `error`: ERROR with Cursor's message (`RunResult.error` or the `status` event) and `cursor.run.error_code`. Run `cancelled`: ERROR `cancelled`, `cursor.run.cancelled=true`.
- `send()`, `stream()` or `wait()` throwing: ERROR on the run span with the exception recorded and the SDK error fields (`cursor.error.name`, `.code`, `.http_status`, `.retryable`, `.operation`). The caller gets the same error.
- `Agent.create()` / `Agent.resume()` throwing (for example `AuthenticationError` for a bad API key): one ERROR span `Cursor Agent create` / `Cursor Agent resume`. A successful create emits no span.
- A `tool_call` with `status: "error"`: ERROR on the tool span.

### When the run span closes

At the first of: `wait()` resolving or throwing, the stream throwing, the stream ending cleanly, or a terminal status from `run.onDidChangeStatus`. While your code is reading the stream, closing waits for the stream to end, so buffered tool events are not lost.

- Stop iterating early (`break`): open tool spans end with `cursor.tool.incomplete=true`, the run span gets `cursor.run.stream_detached=true` and closes when the run ends.
- `wait()` only, or `Agent.prompt()`: one run span with status, model and tokens, no child spans.
- Neither stream nor wait: the run span closes when the run ends (status listener), after a short grace period (`runEndGraceMs`, default 2000) in which you can still start reading the stream.

## Token usage

The run span carries the SDK's cumulative run usage (`RunResult.usage` or `run.usage`) when it is present, otherwise the sum of the run's `usage` events (`cursor.usage.source` says which). Mapping: `inputTokens` to `gen_ai.usage.input_tokens`, `outputTokens` to `gen_ai.usage.output_tokens`, `totalTokens` to `gen_ai.usage.total_tokens` (as reported: Cursor's total excludes reasoning tokens), `cacheReadTokens` and `cacheWriteTokens` to the cache keys, `reasoningTokens` to `gen_ai.usage.output_tokens.reasoning`. Child spans carry no tokens, so trace totals are not double counted. Dollar cost is not on the stream; use `agent.getUsage()` for it.

## Privacy: content is off by default

By default no prompt, tool argument, tool result, thinking text, assistant text or task summary is exported: only names, ids, sizes (`cursor.input.length`, `cursor.text.length`), status, model and tokens. The API key is never read.

Turn content on in code:

```ts
new CursorSDKInstrumentation({ tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
```

or with the fi-core environment variables `FI_HIDE_INPUTS=false` (prompts, tool arguments) and `FI_HIDE_OUTPUTS=false` (results, thinking, assistant text). Only an explicit `false` turns content on. Text values are cut at 5000 characters.

## Flushing

Call `await shutdown(tracerProvider)` (or `instrumentation.forceFlush()`) before a short script exits. It closes runs that are waiting in their grace period and flushes the provider.

## Troubleshooting

- No spans: call `manuallyInstrument(CursorSDK)` before `Agent.create()`. With ESM, the automatic require hook does not see `import`.
- Only a `Cursor Agent create` span with `Invalid User API Key`: `CURSOR_API_KEY` is missing or wrong. Create a user API key at cursor.com/dashboard, API Keys.
- Run span but no tool spans: your code did not read `run.stream()`, or read it after the run span closed. Read the stream before `wait()`.
- Spans missing at exit: await `shutdown()`.

## Tests

```bash
pnpm test                           # jest: fake SDK with the real 1.0.37 shapes and control flow
HOME="$(mktemp -d)" pnpm run test:contract
```

`test:contract` builds the package, type-checks the real `@cursor/sdk` types against the structural types the wrapper reads (`contract/sdk_types.ts`), checks the CommonJS auto-instrumentation hook, and runs the real SDK with an invalid API key (network to api.cursor.com, no account needed).
