# @traceai/codex-sdk

OpenTelemetry tracing for the [OpenAI Codex SDK](https://www.npmjs.com/package/@openai/codex-sdk) (`@openai/codex-sdk`), exported to Future AGI through `@traceai/fi-core`. Every `Thread.run()` and `Thread.runStreamed()` becomes one trace: a `codex.turn` span with one child span per thread item (shell commands, file changes, MCP tool calls, web searches, reasoning, agent messages, to-do lists, errors).

Alpha. Tested against `@openai/codex-sdk` 0.162.1 driving the real `codex` 0.162.1 binary, and against the real SDK driving a stand-in binary (`contract/`).

This is not Codex application session tracing (the `codex` TUI or IDE extension exporting through `[otel]` in `config.toml`), and not the OpenAI API instrumentation (`@traceai/openai`). The SDK runs the `codex` binary in a subprocess, so model requests made inside it are not visible to `@traceai/openai`.

| Item | Value |
|---|---|
| Tested pin | `@openai/codex-sdk` 0.162.1 (Apache-2.0) with its bundled `@openai/codex` 0.162.1 |
| Peer range | `>=0.44.0 <1` (every stable release from 0.44.0 to 0.162.1 has the `Thread` shape this package patches; `src/__tests__/sdkShape.test.ts` checks the installed one) |
| Node | `>=18`, same as the SDK |

## Install

```bash
npm install @traceai/codex-sdk @traceai/fi-core @openai/codex-sdk
```

## Use

```typescript
import * as CodexSDK from "@openai/codex-sdk";
import { register, ProjectType } from "@traceai/fi-core";
import { CodexSDKInstrumentation, shutdown } from "@traceai/codex-sdk";

const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  projectName: "my-codex-app", // or FI_PROJECT_NAME; the collector drops a batch with no project_name
});

new CodexSDKInstrumentation({ tracerProvider }).manuallyInstrument(CodexSDK);

const thread = new CodexSDK.Codex().startThread({ workingDirectory: "/path/to/repo" });
const turn = await thread.run("List the files in this folder and read README.md");

await shutdown(tracerProvider); // flush before a short script exits
```

`FI_API_KEY` and `FI_SECRET_KEY` are read by fi-core and sent as the `X-Api-Key` / `X-Secret-Key` headers. They are never copied onto spans.

The SDK is ESM-only, so its module namespace cannot be patched; its `Thread` class can. `manuallyInstrument(CodexSDK)` patches `Thread.prototype` once for the whole process. To trace only some clients instead, wrap instances:

```typescript
const instrumentation = new CodexSDKInstrumentation({ tracerProvider });
const codex = instrumentation.wrapCodex(new CodexSDK.Codex()); // every thread it starts or resumes
const thread = instrumentation.wrapThread(existingThread);      // one thread
instrumentation.unpatch();                                      // undo everything this object patched
```

`instrumentCodexSDK(CodexSDK, { tracerProvider })` is the one-call form.

### What the wrapper does and does not do

- `run()` and `runStreamed()` both drive the SDK's private `Thread.runStreamedInternal()` generator. The wrapper wraps that one method, so both entry points are traced with real per-event timing. It yields the SDK's own event objects, unchanged and in order, and forwards `return()` / `throw()`: `run()` returns exactly what it returned before, and throws the same errors. If a future SDK drops that method, the wrapper falls back to `runStreamed()` and logs a warning that `run()` may be untraced.
- `Codex` options (`codexPathOverride`, `baseUrl`, `apiKey`, `config`, `env`) and thread options pass through untouched.
- A tracing error never fails the agent. An exporter or collector failure is logged and does not reject the stream.

## Spans

```
codex.turn                     AGENT   one per run() / runStreamed()
├─ codex.reasoning             CHAIN
├─ codex.agent_message         LLM
├─ codex.command_execution     TOOL
├─ codex.file_change           TOOL
├─ codex.mcp_tool_call         TOOL
├─ codex.web_search            TOOL
├─ codex.todo_list             CHAIN
└─ codex.error                 CHAIN   status ERROR (non-fatal error item)
```

`fi.span.kind` and `gen_ai.span.kind` carry the kind. An item type a newer SDK adds becomes `codex.<type>` (CHAIN).

| Field | Attributes | Source |
|---|---|---|
| Session | `session.id`, `gen_ai.conversation.id`, `codex.thread.id`; `codex.thread.resumed` | `thread.started.thread_id`, or the id of a resumed thread. Every child span carries `session.id` too |
| Model | `gen_ai.request.model` on the turn and on agent messages | `ThreadOptions.model`, when set. The SDK does not report which model the binary used, so a model chosen in `config.toml` is not recorded |
| Provider | `gen_ai.provider.name` = `openai` | |
| Tokens | `gen_ai.usage.input_tokens`, `output_tokens`, `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens.reasoning` | `turn.completed.usage`, on the turn span only. See "Token usage" |
| Thread options | `codex.sandbox_mode`, `codex.approval_policy`, `codex.model_reasoning_effort`, `codex.web_search_mode` | `ThreadOptions` |
| Items | `codex.item.id`, `codex.item.type`, `codex.item.status`, `tool.name`, `gen_ai.tool.call.id` | `item.*` events |
| Commands | `codex.command.exit_code`, `codex.command.output_length` | `command_execution` |
| File changes | `codex.file_change.count`, `codex.file_change.kinds` (`add`, `delete`, `update`) | `file_change` |
| MCP | `tool.name` = the MCP tool, `codex.mcp.server`, `codex.mcp.tool` | `mcp_tool_call` |
| To-dos | `codex.todo.count`, `codex.todo.completed` | `todo_list` (started, updated, completed with the turn) |
| Errors | span status ERROR; `codex.error.message`; `codex.cancelled=true` | `turn.failed`, `error` event, a thrown error (for example `Codex Exec exited with code 1`), `failed` items, `error` items, abort |

Timing comes from when each event reaches your process. `agent_message` and `reasoning` items arrive only as `item.completed`; their spans start at the previous event (`codex.item.started_event=false`). The binary reports a command's `item.started` and `item.completed` close together, so command spans are short.

A fi-core `session.id` the app set (fi-core context helpers) is kept; the thread id still goes to `codex.thread.id`.

## Token usage

`turn.completed.usage` is the thread's running total, not the turn's own spend. Measured on 0.162.1: a second turn on a thread reported 39,318 input tokens after the first reported 19,378, and a third turn resumed from a new process reported 49,397. The Future AGI collector sums `gen_ai.usage.*` over a trace and over a `session.id`, so the turn span holds only the new spend:

- A new thread's first turn promotes the full value.
- Later turns promote the total minus the last total this process saw for that thread id (an in-process map, at most 1000 threads). `resumeThread(id)` in the same process uses it too.
- A resumed thread with no total seen in this process (a resume after a restart, or on another worker) promotes nothing on its first turn and sets `codex.usage.baseline_unknown=true`. Its later turns promote their difference.
- If the total goes down, the reported value is promoted as is and `codex.usage.non_monotonic=true` is set.
- The running totals are always on `codex.cumulative.input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`.

`input_tokens` includes cached tokens and `output_tokens` includes reasoning tokens (OpenAI accounting); `total_tokens` is their sum. No cost is computed here.

## Privacy: content is off by default

Prompts, shell commands, command output, file paths, MCP arguments and results, web search queries, reasoning, agent messages and to-do text can contain source code or secrets. They are **not** recorded unless you opt in:

```typescript
new CodexSDKInstrumentation({ tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
// or: FI_HIDE_INPUTS=false FI_HIDE_OUTPUTS=false
```

With inputs visible, the turn's `input.value` is the prompt and tool spans carry the command, file changes, MCP arguments or search query. With outputs visible, the turn's `output.value` is the final agent message and items carry their output. Precedence: `traceConfig`, then the env var, then hidden. The env vars fail closed: only `false` (trimmed, any case) turns capture on; `1`, `yes`, `true` or an empty value keep content hidden. Structural fields (lengths, counts, exit codes, statuses, tool and MCP server names) are always recorded. An MCP tool's error text is used as the span status message only when outputs are visible.

## Flushing

`shutdown(provider)` calls `forceFlush()` on the provider. It does not shut the provider down.

## Troubleshooting

- **No traces.** `register()` needs `projectName` or `FI_PROJECT_NAME`, and `manuallyInstrument()` must get the module namespace (`import * as CodexSDK`), not the `Codex` class.
- **Zero spans from a short script.** `await shutdown(provider)` before exit.
- **No tokens on a turn.** Look for `codex.usage.baseline_unknown=true`: the thread was resumed with no earlier total in this process. `codex.cumulative.*` still has the totals.
- **No model.** Pass `model` in `startThread()` / `resumeThread()` options.
- **Spans marked cancelled.** The caller stopped reading `runStreamed()` events before `turn.completed`, or aborted with `TurnOptions.signal`.

## Tests

```bash
pnpm --filter @traceai/codex-sdk test            # jest: span model, privacy, usage, patching, SDK shape
pnpm --filter @traceai/codex-sdk test:contract   # build, then the real SDK against contract/fake-codex.mjs
```
