# @traceai/pi-coding-agent

OpenTelemetry tracing for the [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk) (`createAgentSession()` / `AgentSession`), exported to Future AGI through `@traceai/fi-core`.

Alpha. Tested against `@earendil-works/pi-coding-agent` 1.2.0 with recorded event sequences and with a real SDK session (OpenAI-compatible endpoint, built-in `ls` / `read` tools) exported to a local Future AGI collector.

This is the in-process SDK integration. It is not the Pi command-line app (`pi`, traced with a Pi extension) and not the Oh My Pi fork (`omp`). Pi's own model calls are not instrumented a second time: a provider instrumentor (`@traceai/openai`, `@traceai/anthropic`) in the same process can add duplicate model spans if it instruments the SDK client Pi loads. If the Pi extension for the command-line app is installed in `~/.pi/agent/extensions/` or `.pi/extensions/`, an SDK session that calls `bindExtensions()` loads it too and every prompt is traced twice; see Troubleshooting.

| Item | Value |
|---|---|
| Tested SDK | `@earendil-works/pi-coding-agent` 1.2.0 (Node 22.19+) |
| Supported Pi versions | **0.80.5 and later (including 1.x):** a run ends at `agent_settled`, after Pi's retries, compaction and continuations. 0.80.5 is the first published version with `agent_settled`. **0.75.4 to 0.80.3:** Pi had no `agent_settled`, so a run ends at the first `agent_end` whose `willRetry` is not `true`. Automatic retries stay in the run; if Pi continues after that `agent_end` (overflow-compaction recovery, messages queued by `agent_end` handlers), the continuation becomes a second trace, and a compaction Pi runs after that `agent_end` is its own one-span trace. **Before 0.75.4 (including every `@mariozechner/pi-coding-agent` release):** `agent_end` has no `willRetry`, so each automatic retry also becomes its own trace. |
| Package scope | Pi moved from `@mariozechner/*` to `@earendil-works/*` at 0.74.0. The package reads the session object structurally and has no dependency on the SDK. |
| SDK surface used | `AgentSession.sessionId`, `AgentSession.sessionName` (only with `hideInputs: false`), `AgentSession.subscribe()`, `AgentSession.dispose()`, and whether `AgentSession.waitForIdle` exists (never called: Pi added it together with `agent_settled`, so its presence means runs end at `agent_settled`) |

## Install

```bash
npm install @traceai/pi-coding-agent @traceai/fi-core @earendil-works/pi-coding-agent
```

## Use

The SDK is ESM-only and its module namespace cannot be patched. Wrap `createAgentSession`, or pass each session to `instrumentSession()` before you prompt it:

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { createAgentSession, SessionManager } from "@earendil-works/pi-coding-agent";
import { PiCodingAgentInstrumentation } from "@traceai/pi-coding-agent";

const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  projectName: "my-pi-agent", // or FI_PROJECT_NAME
});
const instrumentation = new PiCodingAgentInstrumentation({ tracerProvider });

const { session } = await instrumentation.wrapCreateAgentSession(createAgentSession)({
  sessionManager: SessionManager.inMemory(),
});
// or: const { session } = await createAgentSession(...); instrumentation.instrumentSession(session);

try {
  await session.prompt("List the files in this folder and read README.md");
} finally {
  session.dispose();            // ends any span still open
  await tracerProvider.shutdown(); // flush before a short script exits
}
```

`FI_API_KEY` and `FI_SECRET_KEY` are read by fi-core and sent as the `X-Api-Key` / `X-Secret-Key` headers. They are never copied onto spans.

`instrumentSession()` returns a function that stops tracing that session. One instrumentation per session: while a session is traced, a second `instrumentSession()` call (from any instrumentation, whatever its config) returns the first stop function and does not subscribe again. After the stop function, calling `instrumentSession()` again traces the session again. An `AgentSessionRuntime` replaces its `AgentSession` on new / resume / fork / import; call `instrumentSession(runtime.session)` again after each replacement, as Pi's own docs say for any subscription.

## Spans

One trace per agent run (one `session.prompt()`, including Pi's automatic retries and the compaction Pi runs after a model call; see "Supported Pi versions" for Pi before 0.80.5). Every span carries `session.id` = `session.sessionId`, so the runs of one session group together in Future AGI Sessions.

| Span name | `fi.span.kind` / `gen_ai.span.kind` | From | Parent |
|---|---|---|---|
| `invoke_agent pi` | `AGENT` | `agent_start` .. `agent_settled` (Pi before 0.80.5: .. the first `agent_end` without `willRetry: true`) | the active span at `agent_start` when the app registered a context manager (for example `AsyncLocalStorageContextManager`), else none (a new trace) |
| `pi.turn` | `CHAIN` | `turn_start` .. `turn_end` | run |
| `chat <model>` | `LLM` | assistant `message_start` .. `message_end` (start/end from the message's `timestamp` + `durationMs`) | turn |
| `execute_tool <tool>` | `TOOL` | `tool_execution_start` .. `tool_execution_end` | turn, or the calling tool when the event has `parentToolCallId` |
| `pi.compaction` | `CHAIN` | `compaction_start` .. `compaction_end` | run, when Pi compacts inside a run (threshold or overflow compaction after a model call). A compaction outside a run (`session.compact()`, or the check Pi runs before sending a prompt) is its own one-span trace with the same `session.id` |

| Field | Attributes | Source |
|---|---|---|
| Session | `session.id`, `pi.session.id`, `gen_ai.conversation.id`; `pi.session.name` only with `hideInputs: false` (a user-chosen name) | `session.sessionId`, `session.sessionName` |
| Model | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.provider.name`, `pi.api`, `pi.thinking_level` | `AssistantMessage` |
| Tokens | `gen_ai.usage.input_tokens` (= Pi `input` + `cacheRead` + `cacheWrite`), `output_tokens`, `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens.reasoning`; `pi.usage.uncached_input_tokens` (Pi's own `input`) | `AssistantMessage.usage`, LLM spans only |
| Cost | `gen_ai.cost.total` | `usage.cost.total`, only when Pi priced the call (> 0). Models from `models.json` without prices report 0 and get no cost key |
| Finish | `gen_ai.response.finish_reasons` (Pi's `stopReason`, with `toolUse` reported as `tool_calls`), `pi.stop_reason` (Pi's own value), `pi.llm.tool_call_names`, `pi.llm.tool_call_count`. Both reasons are left out while a message is still streaming (`pending`) | `stopReason`, `toolCall` parts |
| Tools | `gen_ai.tool.name`, `tool.name`, `gen_ai.tool.call.id`, `tool_call.id`, `pi.tool.duration_ms`, `pi.tool.is_error`, `pi.tool.parent_call_id` | tool execution events |
| Run totals | `pi.agent.attempts` (agent loops in the run: the first, each automatic retry, and each continuation such as overflow-compaction recovery or queued messages), `pi.agent.retry_count` (automatic retries), `pi.agent.turn_count`, `llm_call_count`, `tool_call_count`, `pi.agent.usage.*`, `pi.agent.cost.total` | summed over the run's LLM and compaction spans. Not the promoted `gen_ai.usage.*` keys, so tokens are not counted twice |
| Compaction | `pi.compaction.reason` (`manual`, `threshold`, `overflow`), `pi.compaction.tokens_before`, `pi.compaction.estimated_tokens_after`, `pi.compaction.will_retry`, and the summarization call's `gen_ai.usage.*` / `gen_ai.cost.total` | `compaction_end.result` |
| Retries | span events `pi.auto_retry.start` / `.end` on the run span | session events |
| Errors | `error.type` on ERROR spans: the provider's error type (for example `invalid_request_error`), else the HTTP status, else `error`; `aborted`, `tool_error`, `compaction_error` | see below |

Errors:

- An assistant message with `stopReason` `error` or `aborted` makes its LLM span and turn ERROR. By default the status message is built only from the stop reason, an HTTP status at the start of Pi's `errorMessage`, and the provider's error `type`, for example `error: HTTP 400 invalid_request_error` or `aborted`. The provider's raw error text (truncated to 1000 characters) is the status message only with `hideOutputs: false`: gateways and providers echo request fragments, account details and local paths in it. `aborted` also sets `pi.aborted=true`.
- The run span is ERROR when the run ends aborted (`agent_settled.aborted`) or its last model call failed, with the same status message as that call. A failed call that Pi retried successfully leaves the run OK; the failed call's own span stays ERROR.
- A tool result with `isError: true` makes the tool span ERROR. The status message is the tool's error text only with `hideOutputs: false`; otherwise "tool reported an error (result hidden)".
- A compaction that fails is ERROR with "compaction failed" (Pi's error text only with `hideOutputs: false`); an aborted one is ERROR with "aborted".
- `session.dispose()` or the stop function during a run ends every open span with `pi.incomplete=true`, children before their parents.

Not available from the SDK's session events, so not recorded: the provider request payload (the full transcript sent on each model call), streaming deltas and time to first token, a separate per-request HTTP status attribute (only what Pi's error text starts with), the model that wrote a compaction summary, and the system prompt.

## Privacy: content is off by default

Prompts, tool arguments, tool results and assistant text of a coding agent contain source code, file contents and command output. They are **not** recorded unless you opt in.

Always exported: session id, model, provider and API, token counts and cost, stop and finish reasons, tool names and call ids, durations, turn/call counts, compaction reason and token counts, and error statuses built from the stop reason, HTTP status and provider error type. The Pi working directory is never added as an attribute.

To opt in:

```typescript
new PiCodingAgentInstrumentation({ tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
// or: FI_HIDE_INPUTS=false FI_HIDE_OUTPUTS=false
```

| Setting | Adds |
|---|---|
| `hideInputs: false` | the user prompt (`input.value` on the run span), tool arguments (`input.value` on tool spans), tool-call arguments inside the LLM output, the session name (`pi.session.name`) |
| `hideOutputs: false` | assistant text (`output.value` on LLM and run spans), tool results (`output.value` on tool spans), the compaction summary (`output.value` on `pi.compaction`), and the raw error text in the status: provider errors (up to 1000 characters), tool errors, compaction errors |

With content on, tool arguments, tool results and error text usually contain absolute file paths, which include your username.

Precedence: `traceConfig` option, then the env var, then hidden. The env vars fail closed: only `false` (any case, surrounding spaces ignored) turns capture on. Values are cut at `maxContentLength` characters (default 16384). The system prompt is never exported.

## Flushing

`tracerProvider.shutdown()` (or this package's `shutdown()`, which calls `forceFlush()` on the providers it knows) before a short script exits. A tracing error never fails the session: every event handler catches and logs through the OpenTelemetry diag logger.

## Troubleshooting

- **`FI_PROJECT_NAME is not set`.** `register()` throws without `projectName` or `FI_PROJECT_NAME`.
- **No traces.** Check `FI_API_KEY` / `FI_SECRET_KEY` and, for a self-hosted Future AGI, `FI_BASE_URL`.
- **Nothing from a short script.** Await `tracerProvider.shutdown()` before exit.
- **Spans stop after `/new`, `/resume` or a fork.** The runtime made a new `AgentSession`; instrument it.
- **Duplicate model spans.** A provider instrumentor in the same process can add a second model span if it instruments the `openai` / `@anthropic-ai/sdk` client that Pi loads.
- **Every prompt appears as two traces** (one from scope `@traceai/pi-coding-agent`, one from the Pi extension's `futureagi.pi`). The Pi extension for the command-line app is installed in `~/.pi/agent/extensions/` (or `$PI_CODING_AGENT_DIR/extensions/`) or the project's `.pi/extensions/`, and the app calls `session.bindExtensions()`, which starts it. Use one or the other, or create the session with a `DefaultResourceLoader` that leaves the extension out: `noExtensions: true`, or `extensionsOverride: (base) => ({ ...base, extensions: base.extensions.filter((e) => !e.path.endsWith("futureagi-otlp.ts")) })`, then `await loader.reload()` and pass `resourceLoader: loader` to `createAgentSession()`.
- **Runs merge or split on old Pi.** See "Supported Pi versions": before 0.80.5 Pi has no `agent_settled`.

## Tests

```bash
pnpm --filter @traceai/pi-coding-agent test
```
