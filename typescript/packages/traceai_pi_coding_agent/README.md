# @traceai/pi-coding-agent

OpenTelemetry tracing for the [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk) (`createAgentSession()` / `AgentSession`), exported to Future AGI through `@traceai/fi-core`.

Alpha. Tested against `@earendil-works/pi-coding-agent` 1.2.0 with recorded event sequences and with a real SDK session (OpenAI-compatible endpoint, built-in `ls` / `read` tools) exported to a local Future AGI collector.

This is the in-process SDK integration. It is not the Pi command-line app (`pi`, traced with a Pi extension) and not the Oh My Pi fork (`omp`). Pi's own model calls are not instrumented a second time: do not also run a provider instrumentor (`@traceai/openai`, `@traceai/anthropic`) on the same process unless you want duplicate model spans.

| Item | Value |
|---|---|
| Tested SDK | `@earendil-works/pi-coding-agent` 1.2.0 (Node 22.19+) |
| Package scope | Pi moved from `@mariozechner/*` to `@earendil-works/*` at 0.74.0. Sessions from either scope work: the package reads the session object structurally and has no dependency on the SDK. |
| SDK surface used | `AgentSession.sessionId`, `AgentSession.subscribe()`, `AgentSession.dispose()` |

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

`instrumentSession()` returns a function that stops tracing that session. Calling it twice for one session is a no-op. An `AgentSessionRuntime` replaces its `AgentSession` on new / resume / fork / import; call `instrumentSession(runtime.session)` again after each replacement, as Pi's own docs say for any subscription.

## Spans

One trace per agent run (one `session.prompt()`, including Pi's automatic retries and compaction). Every span carries `session.id` = `session.sessionId`, so the runs of one session group together in Future AGI Sessions.

| Span name | `fi.span.kind` / `gen_ai.span.kind` | From | Parent |
|---|---|---|---|
| `invoke_agent pi` | `AGENT` | `agent_start` .. `agent_settled` | the active span at `agent_start` when the app registered a context manager (for example `AsyncLocalStorageContextManager`), else none (a new trace) |
| `pi.turn` | `CHAIN` | `turn_start` .. `turn_end` | run |
| `chat <model>` | `LLM` | assistant `message_start` .. `message_end` (start/end from the message's `timestamp` + `durationMs`) | turn |
| `execute_tool <tool>` | `TOOL` | `tool_execution_start` .. `tool_execution_end` | turn, or the calling tool when the event has `parentToolCallId` |

| Field | Attributes | Source |
|---|---|---|
| Session | `session.id`, `pi.session.id`, `gen_ai.conversation.id`, `pi.session.name` | `session.sessionId`, `session.sessionName` |
| Model | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.provider.name`, `pi.api`, `pi.thinking_level` | `AssistantMessage` |
| Tokens | `gen_ai.usage.input_tokens` (= Pi `input` + `cacheRead` + `cacheWrite`), `output_tokens`, `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens.reasoning`; `pi.usage.uncached_input_tokens` (Pi's own `input`) | `AssistantMessage.usage`, LLM spans only |
| Cost | `gen_ai.cost.total` | `usage.cost.total`, only when Pi priced the call (> 0). Models from `models.json` without prices report 0 and get no cost key |
| Finish | `gen_ai.response.finish_reasons`, `pi.stop_reason`, `pi.llm.tool_call_names`, `pi.llm.tool_call_count` | `stopReason`, `toolCall` parts |
| Tools | `gen_ai.tool.name`, `tool.name`, `gen_ai.tool.call.id`, `tool_call.id`, `pi.tool.duration_ms`, `pi.tool.is_error`, `pi.tool.parent_call_id` | tool execution events |
| Run totals | `pi.agent.attempts`, `turn_count`, `llm_call_count`, `tool_call_count`, `pi.agent.usage.*`, `pi.agent.cost.total` | summed LLM spans. Not the promoted `gen_ai.usage.*` keys, so tokens are not counted twice |
| Retries / compaction | span events `pi.auto_retry.start` / `.end`, `pi.compaction.start` / `.end` on the run span | session events |

Errors:

- An assistant message with `stopReason` `error` or `aborted` makes its LLM span and turn ERROR, with the provider's `errorMessage` as the status message (truncated to 1000 characters). `aborted` also sets `pi.aborted=true`.
- The run span is ERROR when the run ends aborted (`agent_settled.aborted`) or its last model call failed. A failed call that Pi retried successfully leaves the run OK; the failed call's own span stays ERROR.
- A tool result with `isError: true` makes the tool span ERROR. The status message is the tool's error text only when outputs are on; otherwise "tool reported an error (result hidden)".
- `session.dispose()` or the stop function during a run ends every open span with `pi.incomplete=true`.

Not available from the SDK's session events, so not recorded: the provider request payload (the full transcript sent on each model call), streaming deltas and time to first token, per-request HTTP status, and the system prompt.

## Privacy: content is off by default

Prompts, tool arguments, tool results and assistant text of a coding agent contain source code, file contents and command output. They are **not** recorded unless you opt in:

```typescript
new PiCodingAgentInstrumentation({ tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
// or: FI_HIDE_INPUTS=false FI_HIDE_OUTPUTS=false
```

| Setting | Adds |
|---|---|
| `hideInputs: false` | the user prompt (`input.value` on the run span), tool arguments (`input.value` on tool spans), tool-call arguments inside the LLM output |
| `hideOutputs: false` | assistant text (`output.value` on LLM and run spans), tool results (`output.value` on tool spans), tool error text in the status |

Precedence: `traceConfig` option, then the env var, then hidden. The env vars fail closed: only `false` (trimmed, any case) turns capture on. Values are cut at `maxContentLength` characters (default 16384). The system prompt is never exported.

## Flushing

`tracerProvider.shutdown()` (or this package's `shutdown()`, which calls `forceFlush()` on the providers it knows) before a short script exits. A tracing error never fails the session: every event handler catches and logs through the OpenTelemetry diag logger.

## Troubleshooting

- **No traces.** `register()` needs `projectName` or `FI_PROJECT_NAME`, and `FI_API_KEY` / `FI_SECRET_KEY`.
- **Nothing from a short script.** Await `tracerProvider.shutdown()` before exit.
- **Spans stop after `/new`, `/resume` or a fork.** The runtime made a new `AgentSession`; instrument it.
- **Duplicate model spans.** A provider instrumentor is also tracing Pi's HTTP calls.

## Tests

```bash
pnpm --filter @traceai/pi-coding-agent test
```
