# @traceai/claude-agent-sdk

OpenTelemetry tracing for the TypeScript [Claude Agent SDK](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk) `query()`, exported to Future AGI through `@traceai/fi-core`.

Alpha. Tested against `@anthropic-ai/claude-agent-sdk` 0.3.289 with recorded message fixtures and with the real SDK and bundled CLI pointed at a loopback mock of the Messages API. No live Anthropic call was made.

This is not the Python package (`traceAI-claude-agent-sdk`), not Claude Code CLI session tracing, and not the Anthropic Messages API instrumentation (`@traceai/anthropic`).

## Licence of the SDK

`@anthropic-ai/claude-agent-sdk` is under the Anthropic Commercial Terms, not an OSI licence. It is a **peer dependency**: install it yourself. This package never bundles or re-exports the SDK or its CLI binary, and does not import it at runtime.

| Item | Value |
|---|---|
| Tested pin | `@anthropic-ai/claude-agent-sdk` 0.3.289 (npm shasum `36c710c40a2d9761d6c12c42b469d002e64e9be8`) |
| Peer range | `^0.3.142` (0.3.142 is the oldest 0.3.x on npm; its `query()` and the message fields read here match 0.3.289, and the real-SDK contract test passed against it) |
| Node | 18, 20, 22 (CJS and ESM import checked); the SDK itself declares `node >=18` |

## Install

```bash
npm install @traceai/claude-agent-sdk @traceai/fi-core @anthropic-ai/claude-agent-sdk
```

## Use

The SDK is ESM-only and its module namespace cannot be patched, so wrap `query` and call the wrapped function:

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { wrapQuery, shutdown } from "@traceai/claude-agent-sdk";

const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  projectName: "my-agent", // or FI_PROJECT_NAME; the collector drops a batch with no project_name
});

const tracedQuery = wrapQuery(query, { tracerProvider });

for await (const message of tracedQuery({ prompt: "Summarize the README.", options: { maxTurns: 3 } })) {
  // the same message objects the SDK yielded
}

await shutdown(); // flush before a short script exits
```

`FI_API_KEY` and `FI_SECRET_KEY` are read by fi-core and sent as the `X-Api-Key` / `X-Secret-Key` headers. They are never copied onto spans.

`ClaudeAgentSDKInstrumentation` (or `instrumentClaudeAgentSDK()`) does the same with an object you keep: `instrumentation.wrapQuery(query)`, `instrumentation.forceFlush()`.

### What the wrapper does and does not do

- Calls the original `query()` with the same params object. Options (`resume`, `forkSession`, `sessionId`, `hooks`, `mcpServers`, `agents`, `abortController`, `allowedTools`, `permissionMode`, `maxTurns`, `env`, ...) are passed through untouched. The wrapper never sets `ANTHROPIC_BASE_URL`.
- Yields the same message objects, in order. Control methods on the returned `Query` (`interrupt()`, `setModel()`, ...) go to the original.
- A tracing error never fails the agent. An exporter or collector failure is logged and does not reject the iterator.
- `startup()` is not wrapped in 0.1.0.

## Spans

| Span name | `claude_agent.span_kind` | `gen_ai.span.kind` / `fi.span.kind` | Parent |
|---|---|---|---|
| `claude_agent.conversation` | `conversation` | `CHAIN` (TS fi-semantic-conventions has no `CONVERSATION`) | caller's active span |
| `claude_agent.assistant_turn` | `assistant_turn` | `LLM` | conversation, or the subagent span inside a subagent |
| `tool.<name>` | `tool_execution`, or `mcp_tool` for `mcp__<server>__<tool>` | `TOOL` | the assistant turn that issued the `tool_use` |
| `claude_agent.subagent.<subagent_type>` | `subagent` | `AGENT` | the `tool.Agent` / `tool.Task` span |

One API response that the SDK splits over several assistant messages (same `message.id`) is one turn.

| Field | Attributes | Source |
|---|---|---|
| Model | `gen_ai.request.model`, `claude_agent.model` | `options.model`, the init message, each assistant message |
| Provider | `gen_ai.provider.name` = `anthropic`, or `custom` when `ANTHROPIC_BASE_URL` (from `options.env`, else `process.env`) is not an `anthropic.com` host | |
| Tokens | `gen_ai.usage.input_tokens`, `output_tokens`, `total_tokens`; `gen_ai.usage.cache_read_tokens` / `cache_creation_tokens` when present | result message, on the conversation span |
| Cost | `claude_agent.cost.total_usd` and `gen_ai.cost.total` | result `total_cost_usd`; never computed here |
| Session | `session.id`, `claude_agent.session.id`, `claude_agent.session_id`; `claude_agent.session.fork_from` on fork | init / result `session_id`, `options.sessionId`, `options.resume` |
| Tools | `claude_agent.tool.name`, `gen_ai.tool.name`, `use_id`, `source` (`builtin` / `mcp` / `custom`), `is_error`, `duration_ms` | `tool_use` / `tool_result` blocks |
| Errors | span status ERROR; `claude_agent.error.type` / `error.message`; `claude_agent.cancelled=true` on abort | error result, thrown error, `is_error` tool results, `AbortController` |

The `claude_agent.*` names are the Python package's names (`_attributes.py`); a test fails if one is missing.

An app-set fi-core `session.id` / `user.id` (fi-core context helpers) is kept; the SDK session id still goes to `claude_agent.session.id`.

## Privacy: content is off by default

Prompts, system prompts, tool inputs, tool outputs, assistant text and the final result can contain source code, file contents or secrets. They are **not** recorded unless you opt in:

```typescript
wrapQuery(query, { tracerProvider, traceConfig: { hideInputs: false, hideOutputs: false } });
// or: FI_HIDE_INPUTS=false FI_HIDE_OUTPUTS=false
```

Precedence: `traceConfig` option, then the env var, then hidden. The env vars fail closed: only the value `false` (trimmed, any case) turns capture on. Any other value, including `1`, `yes`, `true`, ` true` or an empty string, keeps content hidden. (fi-core itself treats every value other than `true` as "show", so `FI_HIDE_INPUTS=1` would capture there; this package does not follow that rule.)

This is stricter than fi-core's own default and than the Python package, which records the prompt and tool input/output unconditionally.

## Flushing and batching

`shutdown()` calls `forceFlush()` on the provider(s) you passed (or the global provider). It does not shut the provider down. The wrapper has no queue of its own.

fi-core's `register()` exports each span through its default `SimpleSpanProcessor`. At the fi-core version in this repo, `register({ batch: true })` does not replace that processor with OpenTelemetry `sdk-trace-base` 2.x. If you want batching, build a provider with a `BatchSpanProcessor` yourself (`OTEL_BSP_MAX_QUEUE_SIZE`, default 2048).

## Pointing the SDK at a mock or gateway

There is no `baseURL` option. `options.env` **replaces** the CLI subprocess environment, so spread `process.env` (or pass `PATH`, `HOME`, ...) and add `ANTHROPIC_BASE_URL`:

```typescript
options: { env: { ...process.env, ANTHROPIC_BASE_URL: "http://127.0.0.1:8080" } }
```

With 0.3.289 the CLI then calls `POST {ANTHROPIC_BASE_URL}/v1/messages?beta=true` with `stream: true` (observed in `contract/run_real_sdk.mjs`).

## Troubleshooting

- **No traces.** `register()` needs `projectName` or `FI_PROJECT_NAME`.
- **Zero spans from a short script.** `await shutdown()` before exit.
- **Duplicate model spans.** Do not also run a provider instrumentor on the same calls.
- **A span left open after abort.** Every span for the query is ended with status ERROR and `claude_agent.cancelled=true` when its `AbortController` aborts. Anything else is a bug here.

## Tests

```bash
pnpm --filter @traceai/claude-agent-sdk test     # jest: wrapper, parity, fi-core export contract
```

The shared-harness contract tests (`contract/test_harness_contract.py`) run the built package in Node against the Python OTLP `Receiver`, run the real SDK against a loopback Messages API mock, pack the tarball and check it holds no SDK file or native binary, and import the package as CJS and ESM. See the module docstring for the command.
