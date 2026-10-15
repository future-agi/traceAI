# @traceai/cursor-sdk

OpenTelemetry tracing for the [Cursor TypeScript SDK](https://cursor.com/docs/sdk/typescript) (`@cursor/sdk`). Each `agent.send()` becomes one trace in Future AGI: an AGENT span for the run, with child spans for tool calls, thinking and assistant text, the model, token usage and the run status. Every run of one agent shares a session (the agent id), so a multi-turn conversation reads as one session.

This package traces the SDK inside your Node process. It is not the Cursor IDE hook integration, which traces the Cursor editor through `.cursor/hooks.json`.

**What was verified (`@cursor/sdk` 1.0.37, Node 22.18):** against the real package, the `Agent.create` / `Agent.resume` / `Agent.prompt` error paths (wrong key, unknown agent) and a missing-key local run, which drives the real local runtime through `send()`, `stream()` and `wait()` until Cursor rejects it. The tool, thinking, assistant and usage mapping was checked by replaying the SDK's documented stream events (shapes and control flow taken from the 1.0.37 bundle). A live run with a signed-in Cursor account is still pending.

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
    model: { id: "composer-2.5" }, // any id from Cursor.models.list()
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
| `cursor.request.id` | run | `request` event / `run.requestId` |
| `gen_ai.request.model`, `cursor.model.params` | run, assistant | `send({ model })`, `agent.model`, `run.model`, `RunResult.model` |
| `cursor.tools` | run | `system` event, when the runtime sends one |
| `gen_ai.usage.*` | run | see Token usage |
| `cursor.run.status`, `cursor.run.last_status_event`, `cursor.run.duration_ms` | run | `RunResult` / run handle / `status` events |
| `tool.name`, `gen_ai.tool.call.id`, `cursor.tool.status` | tool | `tool_call` events |
| `cursor.tool.args_truncated`, `cursor.tool.result_truncated` | tool | the SDK's `truncated` flags |

Status:

- Run `finished`: OK.
- Run `error`: ERROR, `cursor.run.error_code` from `RunResult.error.code`. Status message `RunError (<code>)` by default; Cursor's own error text only with content opted in (see Privacy).
- Run `cancelled`: ERROR with status message `cancelled` and `cursor.run.cancelled=true` (no Cursor message, no error code).
- `send()`, `stream()` or `wait()` throwing: ERROR on the run span, an `exception` event and the SDK error fields (`cursor.error.name`, `.code`, `.http_status`, `.retryable`, `.operation`). The caller gets the same error.
- `Agent.create()` / `Agent.resume()` throwing (for example `AuthenticationError` for a wrong API key): one ERROR span `Cursor Agent create` / `Cursor Agent resume`. A successful create emits no span.
- A `tool_call` with `status: "error"`: ERROR on the tool span, status message `<tool> failed` by default.

### When the run span closes

At the first of: `wait()` resolving or throwing, the stream throwing, the stream ending cleanly, or a terminal status from `run.onDidChangeStatus`. While your code is reading the stream, closing waits for the stream to end, so buffered tool events are not lost.

- Stop iterating early (`break`): open tool spans end with `cursor.tool.incomplete=true`, the run span gets `cursor.run.stream_detached=true` and closes when the run ends.
- A stream read partly and never closed: once the run's end is known, the span closes after `streamDrainTimeoutMs` (default 30000), marked like an early break.
- `wait()` only, or `Agent.prompt()`: one run span with status, model and tokens, no child spans.
- Neither stream nor wait: the run span closes when the run ends (status listener), after a short grace period (`runEndGraceMs`, default 2000) in which you can still start reading the stream.

## Token usage

The run span carries the SDK's cumulative run usage (`RunResult.usage` or `run.usage`) when it is present, otherwise the sum of the run's `usage` events (`cursor.usage.source` says which). Mapping: `inputTokens` to `gen_ai.usage.input_tokens`, `outputTokens` to `gen_ai.usage.output_tokens`, `totalTokens` to `gen_ai.usage.total_tokens` (as reported: Cursor's total excludes reasoning tokens), `cacheReadTokens` and `cacheWriteTokens` to the cache keys, `reasoningTokens` to `gen_ai.usage.output_tokens.reasoning`. Child spans carry no tokens, so trace totals are not double counted. Dollar cost is not on the stream; use `agent.getUsage()` for it.

## Privacy: content is off by default

By default no prompt, tool argument, tool result, thinking text, assistant text, task summary or error text is exported: only names, ids, sizes (`cursor.input.length`, `cursor.text.length`), status, model and tokens. The API key is never read.

Errors are content too: messages and stack traces carry local paths and usernames, and Cursor's run-error text can quote prompt text. By default an error exports only its class and structural fields:

- the `exception` event has `exception.type` (the class name) and no `exception.message` or `exception.stacktrace`;
- the status message and `cursor.error.message` are a fixed summary, `<ErrorName> (<http status or code>)`, for example `AuthenticationError (401)`, or `RunError (<code>)` for a failed run;
- `cursor.error.name`, `.code`, `.http_status`, `.retryable` and `.operation` are exported as is.

Turn content on in code:

```ts
new CursorSDKInstrumentation({ tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
```

or with the fi-core environment variables `FI_HIDE_INPUTS=false` (prompts, tool arguments) and `FI_HIDE_OUTPUTS=false` (results, thinking, assistant text). Only an explicit `false` turns content on. Error messages, stack traces and Cursor's run-error text are exported only when both are on. Text values are cut at 5000 characters (error messages at 500). With content on, values are exported as the SDK reports them, including file paths inside tool arguments and results.

## Not captured / limitations

- **Child spans without the stream.** Tool, thinking and assistant spans need your code to read `run.stream()`; `wait()`-only runs and `Agent.prompt()` get the run span only. This version does not use the SDK's `onStep` / `onDelta` callbacks.
- **Per-model-call spans.** The SDK reports usage per turn, not per model request; tokens are on the run span only. No cost on spans (use `agent.getUsage()`).
- **Runs not started by a traced `send()`**: runs fetched with `Agent.getRun()` or `Agent.listRuns()` are not traced.
- **System prompt, rules, skills and Cursor's internal context**: the SDK does not expose them on the stream.
- **Subagent internals**: a subagent started through the `task` tool is one tool span.
- **Cloud runtime (`bc-` agents)**: handled by the same code, not run end to end.
- **Runs still open when the process exits** without `shutdown()` / `forceFlush()` are lost; `shutdown()` closes runs still in progress with `cursor.run.status=running` and no status.
- **SDK-internal spans** (`context-tracer` scope) are not filtered by this package; keep the Future AGI provider non-global (see above).
- **A live signed-in run has not been verified yet** (see the top of this file).

## Flushing

Call `await shutdown(tracerProvider)` before a short script exits. It closes every open run span (runs waiting for their final status, partly read streams, runs still in progress) and flushes the provider. `instrumentation.forceFlush()` closes only runs whose end is already known.

## Troubleshooting

- No spans: call `manuallyInstrument(CursorSDK)` before `Agent.create()`. With ESM, the automatic require hook does not see `import`.
- Only a `Cursor Agent create` span, `AuthenticationError (401)`: the API key is wrong (or revoked). Create a user API key at [cursor.com/dashboard/api](https://cursor.com/dashboard/api), or use `Cursor.auth.login()`.
- A `Cursor Agent run` span with status `RunError` (`cursor.run.status=error`) a few seconds after `send()`, and no tool spans: no API key at all (no `apiKey`, no `CURSOR_API_KEY`, no stored login). `Agent.create()` succeeds without a key; the run is rejected once it starts (`[unknown] Invalid User API Key` in `RunResult.error`).
- Run span but no tool spans on a working key: your code did not read `run.stream()`, or read it after the run span closed. Read the stream before or while you call `wait()`.
- Extra traces with names like `LocalGitExecutor.exec`: the provider was registered as the global one. Use `setGlobalTracerProvider: false`.
- Spans missing at exit: await `shutdown()`.

## Tests

```bash
pnpm test                           # jest: fake SDK with the real 1.0.37 shapes and control flow
HOME="$(mktemp -d)" pnpm run test:contract
```

`test:contract` builds the package, type-checks the real `@cursor/sdk` types against the structural types the wrapper reads (`contract/sdk_types.ts`), checks the CommonJS auto-instrumentation hook, and runs the real SDK without an account (network to api.cursor.com): wrong-key `Agent.create` / `Agent.prompt` (identical error to the caller, no message, stack or path exported by default, full text with content on), and a missing-key local run through `send()` / `stream()` / `wait()` that asserts the exporter receives only this package's spans.
