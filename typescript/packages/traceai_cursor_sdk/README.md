# @traceai/cursor-sdk

OpenTelemetry tracing for the [Cursor TypeScript SDK](https://cursor.com/docs/sdk/typescript) (`@cursor/sdk`). Each `agent.send()` becomes one trace in Future AGI: an AGENT span for the run, with child spans for tool calls, thinking and assistant text, the model, token usage and the run status. Every run of one agent shares a session (the agent id), so a multi-turn conversation reads as one session.

This package traces the SDK inside your Node process. It is not the Cursor IDE hook integration, which traces the Cursor editor through `.cursor/hooks.json`.

**What was verified:** live runs with a signed-in Cursor account (Plus plan, `@cursor/sdk` 1.0.38, `composer-2.5`, Node 26.8, local runtime) traced into Future AGI: a streamed run with `read`, `edit` and `shell` tools and thinking, a `wait()`-only follow-up in the same session, and a run the plan rejected (see [Live vendor verification](#live-vendor-verification-2026-10-20)). Against the real package without an account (1.0.37 and 1.0.38): the `Agent.create` / `Agent.resume` / `Agent.prompt` error paths (wrong key, unknown agent) and a missing-key local run. Not run: the cloud runtime (`bc-` agents).

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

// Not the global provider: @cursor/sdk emits its own internal OpenTelemetry spans
// (scope "context-tracer") on the global provider. Pass this one explicitly instead.
const tracerProvider = register({
  projectName: "cursor-sdk-app",
  projectType: ProjectType.OBSERVE,
  setGlobalTracerProvider: false,
});
const instrumentation = new CursorSDKInstrumentation({ tracerProvider });
instrumentation.manuallyInstrument(CursorSDK);

let agent: CursorSDK.SDKAgent | undefined;
try {
  agent = await CursorSDK.Agent.create({
    apiKey: process.env.CURSOR_API_KEY!,
    // A model and its params from Cursor.models.list(); plans with fixed model
    // settings (Plus: fast=false) reject the run otherwise.
    model: { id: "composer-2.5", params: [{ id: "fast", value: "false" }] },
    local: { cwd: process.cwd() },
  });
  const run = await agent.send("List the files in this folder and read README.md");
  for await (const event of run.stream()) {
    // your own handling; events are passed through unchanged
  }
  await run.wait();
} finally {
  await agent?.[Symbol.asyncDispose]();
  await shutdown(tracerProvider); // also exports a failed Agent.create span
}
```

`register()` reads `FI_API_KEY`, `FI_SECRET_KEY` and `FI_BASE_URL`. `@cursor/sdk` is ESM first, so pass the module namespace to `manuallyInstrument()`. A CommonJS app can instead call `registerInstrumentations({ tracerProvider, instrumentations: [new CursorSDKInstrumentation()] })` before `require("@cursor/sdk")`.

`Agent.prompt()` is traced too: it calls `Agent.create()`, `send()` and `wait()` internally. `instrumentation.wrapAgent(agent)` traces an agent you got some other way.

### SDK-internal spans

`@cursor/sdk` bundles `@opentelemetry/api` and starts its own spans (instrumentation scope `context-tracer`, for example `LocalGitExecutor.exec`, `LocalRequestContextExecutor.computeGlobalCache`) on the global tracer provider: 27 of them in one short local run. They are outside this package's privacy settings and carry SDK internals such as file and rule names. With `setGlobalTracerProvider: false` and the provider passed explicitly, as above, none of them reach Future AGI (checked in `contract/real_sdk_error.mjs`). If your app registers its own global provider, those spans go there.

### What the wrapper does and does not do

- It patches the static `Agent.create` and `Agent.resume`, and `send`, `stream` and `wait` on the agent and run objects they return. You get the SDK's own objects back, and `run.stream()` yields the SDK's own events unchanged.
- It never reads the run's event stream on its own. The local runtime's stream is a single-consumer queue, so a second reader would steal your events. In this version, tool, thinking and assistant spans exist only for events your code reads through `run.stream()`. (The SDK's per-send `onStep` / `onDelta` callbacks could feed them without the stream; this version does not use them.)
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
| `session.id` | run and child spans | `agent.agentId` (`agent-<uuid>` local, `bc-<uuid>` cloud), unless your app set a session id through fi-core context; on a create/resume error span only when an agent id was passed |
| `gen_ai.conversation.id`, `cursor.runtime` | run | the agent id; `local` or `cloud` from the id |
| `cursor.agent.id`, `cursor.run.id` | run and child spans | `agent.agentId`, `run.id` |
| `cursor.request.id` | run | `RunResult.requestId` / `run.requestId` (a live local run sends no `request` event; one is used when present) |
| `gen_ai.request.model`, `cursor.model.params` | run, assistant | `send({ model })`, `agent.model`, `run.model`, `RunResult.model` |
| `cursor.tools` | run | `system` event, when the runtime sends one |
| `gen_ai.usage.*` | run | see Token usage |
| `cursor.run.status`, `cursor.run.last_status_event`, `cursor.run.duration_ms` | run | `RunResult` / run handle / `status` events |
| `tool.name`, `gen_ai.tool.call.id`, `cursor.tool.status` | tool | `tool_call` events (ids like `tool_<uuid>`; parallel calls are kept apart by id) |
| `cursor.thinking.duration_ms` | thinking | the block's closing event (empty text, `thinking_duration_ms`): Cursor's own figure for streaming the thinking text. The span starts at the previous event, so it also covers the model's wait before the first delta. |
| `cursor.tool.args_truncated`, `cursor.tool.result_truncated` | tool | the SDK's `truncated` flags |

Status:

- Run `finished`: OK.
- Run `error`: ERROR, `cursor.run.error_code` from `RunResult.error.code`. No status message by default; Cursor's own error text only with content opted in (see Privacy).
- Run `cancelled`: ERROR with `cursor.run.cancelled=true` (status message omitted by default) (no Cursor message, no error code).
- `send()`, `stream()` or `wait()` throwing: ERROR on the run span, an `exception` event and the SDK error fields (`cursor.error.name`, `.code`, `.http_status`, `.retryable`, `.operation`). The caller gets the same error.
- `Agent.create()` / `Agent.resume()` throwing (for example `AuthenticationError` for a wrong API key): one ERROR span `Cursor Agent create` / `Cursor Agent resume`. A successful create emits no span.
- A `tool_call` with `status: "error"`: ERROR on the tool span, with no status message by default.

### When the run span closes

At the first of: `wait()` resolving or throwing, the stream throwing, the stream ending cleanly, or a terminal status from `run.onDidChangeStatus`. While your code is reading the stream, closing waits for the stream to end, so buffered tool events are not lost.

- Stop iterating early (`break`): open tool spans end with `cursor.tool.incomplete=true`, the run span gets `cursor.run.stream_detached=true` and closes when the run ends.
- A stream read partly and never closed: once the run's end is known, the span closes after `streamDrainTimeoutMs` (default 30000), marked like an early break.
- `wait()` only, or `Agent.prompt()`: one run span with status, model and tokens, no child spans.
- Neither stream nor wait: the run span closes when the run ends (status listener), after a short grace period (`runEndGraceMs`, default 2000) in which you can still start reading the stream.

## Token usage

The run span carries the SDK's cumulative run usage (`RunResult.usage` or `run.usage`) when it is present, otherwise the sum of the run's `usage` events (`cursor.usage.source` says which).

Cursor's `inputTokens` excludes cache reads and writes: its `totalTokens` is input + output + cache read + cache write (a live run: 23820 + 258 + 19172 + 0 = 43250). Following the OpenTelemetry GenAI conventions, `gen_ai.usage.input_tokens` is the whole prompt, `inputTokens + cacheReadTokens + cacheWriteTokens` (42992 there), so input + output = total. The other keys: `outputTokens` to `gen_ai.usage.output_tokens`, `totalTokens` to `gen_ai.usage.total_tokens` (as reported; it excludes reasoning tokens), `cacheReadTokens` and `cacheWriteTokens` to the cache keys, `reasoningTokens` (when the model reports them) to `gen_ai.usage.output_tokens.reasoning`. Cursor's own numbers are kept as is in `cursor.usage.input_tokens`, `.output_tokens`, `.cache_read_tokens`, `.cache_write_tokens`, `.total_tokens` and `.reasoning_tokens`. Child spans carry no tokens, so trace totals are not double counted. Dollar cost is not on the stream; `agent.getUsage()` reports it where the account has the feature (it returned `403 feature_unavailable` on the Plus plan).

## Privacy: content is off by default

By default no prompt, tool argument, tool result, thinking text, assistant text, task summary or error text is exported: only names, ids, sizes (`cursor.input.length`, `cursor.text.length`), status, model and tokens. The API key is never read.

Errors are content too: messages and stack traces carry local paths and usernames, and Cursor's run-error text can quote prompt text. By default an error exports only its class and structural fields:

- the `exception` event has `exception.type` (the class name) and no `exception.message` or `exception.stacktrace`;
- fi-core 1.1 removes the status message and `cursor.error.message`, including fixed summaries, because hiding content also enables `hideErrorDetails`;
- `cursor.error.name`, `.code`, `.http_status`, `.retryable` and `.operation` are exported as is.

Turn content on in code:

```ts
new CursorSDKInstrumentation({ tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
```

or with the fi-core environment variables `FI_HIDE_INPUTS=false` (prompts, tool arguments) and `FI_HIDE_OUTPUTS=false` (results, thinking, assistant text). Only an explicit `false` turns content on. Error messages, stack traces and Cursor's run-error text are exported only when both are on and `hideErrorDetails` / `FI_HIDE_ERROR_DETAILS` is not enabled. Setting `hideErrorDetails: false` alone does not opt into raw Cursor error content; it retains fixed summaries while content remains hidden. Text values are cut at 5000 characters (error messages at 500). With content on, values are exported as the SDK reports them, including file paths inside tool arguments and results.

## Not captured / limitations

- **Child spans without the stream.** Tool, thinking and assistant spans need your code to read `run.stream()`; `wait()`-only runs and `Agent.prompt()` get the run span only. This version does not use the SDK's `onStep` / `onDelta` callbacks.
- **Per-model-call spans.** The SDK reports usage per turn, not per model request; tokens are on the run span only. No cost on spans (`agent.getUsage()`, where the plan has it).
- **Runs not started by a traced `send()`**: runs fetched with `Agent.getRun()` or `Agent.listRuns()` are not traced.
- **System prompt, rules, skills and Cursor's internal context**: the SDK does not expose them on the stream.
- **Subagent internals**: a subagent started through the `task` tool is one tool span.
- **Cloud runtime (`bc-` agents)**: handled by the same code, not run end to end (the live runs used the local runtime).
- **Shell exit codes**: Cursor reports a shell command that exits non-zero as a `completed` call with `status: "success"`, so its span is OK; the exit code is only in the result (content on).
- **Runs still open when the process exits** without `shutdown()` / `forceFlush()` are lost; `shutdown()` closes runs still in progress with `cursor.run.status=running` and no status.
- **SDK-internal spans** (`context-tracer` scope) are not filtered by this package; keep the Future AGI provider non-global (see above).

## Flushing

Call `await shutdown(tracerProvider)` before a short script exits. It closes every open run span (runs waiting for their final status, partly read streams, runs still in progress) and flushes the provider. `instrumentation.forceFlush()` closes only runs whose end is already known.

## Troubleshooting

- No spans: call `manuallyInstrument(CursorSDK)` before `Agent.create()`. With ESM, the automatic require hook does not see `import`.
- Only a `Cursor Agent create` span, `AuthenticationError (401)`: the API key is wrong (or revoked). Create a user API key in the Cursor dashboard ([grok.com/dashboard](https://grok.com/dashboard); new keys start with `grok_`), or use `Cursor.auth.login()`.
- A `Cursor Agent run` span with ERROR status and no error code about a second after it starts, with a working key and no tool spans: the model or its settings are not in your plan. With content on, `cursor.error.message` reads `Model unavailable on <plan> ...`. Pass a model and params that `Cursor.models.list()` returns for your key, for example `{ id: "composer-2.5", params: [{ id: "fast", value: "false" }] }` on Plus.
- A `Cursor Agent run` span with ERROR status (`cursor.run.status=error`) a few seconds after `send()`, and no tool spans: no API key at all (no `apiKey`, no `CURSOR_API_KEY`, no stored login). `Agent.create()` succeeds without a key; the run is rejected once it starts (`[unknown] Invalid User API Key` in `RunResult.error`).
- `RunResult.error.message` says `Weekly usage limit reached`: the account has exhausted its allowance. Wait for the reset or add credits in Cursor; changing the tracing settings cannot remove this limit. The example exits non-zero for failed runs.
- A `shell` tool result saying the working directory no longer exists: `local.cwd` was a relative path. Pass an absolute one.
- Run span but no tool spans on a working key: your code did not read `run.stream()`, or read it after the run span closed. Read the stream before or while you call `wait()`.
- Extra traces with names like `LocalGitExecutor.exec`: the provider was registered as the global one. Use `setGlobalTracerProvider: false`.
- Spans missing at exit: await `shutdown()`.

## Tests

```bash
pnpm test                           # jest: fake SDK with the real 1.0.37/1.0.38 shapes and control flow, plus event sequences recorded from live runs
HOME="$(mktemp -d)" pnpm run test:contract
```

`test:contract` builds the package, type-checks the real `@cursor/sdk` types against the structural types the wrapper reads (`contract/sdk_types.ts`), checks the CommonJS auto-instrumentation hook, and runs the real SDK without an account (network to api.cursor.com): wrong-key `Agent.create` / `Agent.prompt` and unknown-agent `Agent.resume` (identical error to the caller, no message, stack or path exported by default, full text with content on), and a missing-key local run through `send()` / `stream()` / `wait()` that asserts the exporter receives only this package's spans.

## Live vendor verification (2026-10-20)

Signed-in Cursor account on the Plus plan, user API key, `@cursor/sdk` 1.0.38 (the npm release), `@traceai/fi-core` 1.0.0 from npm, this package installed from a `pnpm pack` tarball, Node 26.8, local runtime, `composer-2.5` with `fast: false`, a throwaway two-file git repo, a fresh `HOME`, `register({ setGlobalTracerProvider: false })`. Traces landed in a local Future AGI (`fi_verify.py --expect-type tool`: PASS for both projects).

| Run | Trace | Spans |
| --- | --- | --- |
| streamed, default privacy | `eec8abf8c9a94e3c86881f144403c24e` | run OK (23820 input + 19172 cache read / 258 output, total 43250) -> 3 thinking, `read`, `edit`, 1 assistant message |
| `examples/basic.mjs` (before the cwd fix below) with content on (`FI_HIDE_INPUTS=false`, `FI_HIDE_OUTPUTS=false`), run 1 streamed | `581f15b8429948b7830e387361944f5a` | run OK -> 2 thinking, `shell`, `read` (in parallel with the shell), `shell`, 1 assistant message |
| same agent, run 2 `wait()` only | `93c316ab674e4a59aa3c9f9ce9b14543` | run OK, same session as run 1 |
| `composer-2.5` without params on Plus | `9529e2dfaba046829124a4b4b427fcf3` | run ERROR `RunError` (Cursor: "Model unavailable on Plus ... with fixed settings") |

What the real stream showed, compared with the replay this package was first tested on (jest now also replays the recorded sequences):

- No `request`, `user`, `system` or `task` events in a local run; the request id is only on `RunResult.requestId` (already read from there).
- Thinking arrives as text deltas closed by an empty-text event with `thinking_duration_ms`, as assumed; Cursor's duration covers only the streaming of the thinking text.
- Tool calls: `running` (repeated for partial updates with the same args), then `completed`; ids `tool_<uuid>`; no `truncated` field; tools can run in parallel.
- `totalTokens` includes cache reads and writes, and `inputTokens` excludes them (the replay had assumed total = input + output). Fixed: `gen_ai.usage.input_tokens` now adds the cache tokens.
- A failed run can carry `RunResult.error` with a message and no code; fi-core 1.1 omits the default status message; the ERROR status and structural fields remain.
- A relative `local.cwd` breaks the agent's `shell` tool ("working directory no longer exists"); the example now resolves it.
- `agent.getUsage()` returned `403 feature_unavailable` on Plus.
- Privacy, over every stored column: default config, prompt marker 0, README marker 0, home paths 0; content on, the prompt in `input`, the README marker in tool and run outputs, home paths in tool arguments (as documented). API key 0 in both. No SDK-internal (`context-tracer`) span reached Future AGI.
