# traceAI-hermes-agent

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that exports
Hermes turns, model calls and tool calls to Future AGI as OpenTelemetry spans.
It registers observer callbacks on Hermes' documented plugin hooks; it is not
part of Hermes and does not change what Hermes does.

```
hermes.turn            AGENT  one user message to final answer (session, platform, outcome)
├── chat gpt-6-luna    LLM    one provider request: model, provider, tokens, finish reason
├── read_file          TOOL   one tool call: name, call id, duration, status
└── chat gpt-6-luna    LLM
```

One trace per Hermes turn. Every span carries `session.id` (the Hermes session
id), so the turns of a `hermes chat` session group into one Future AGI session.
A subagent's turn is nested under the turn that started it.

Alpha. Tested with Hermes Agent v2026.9.24 (0.21.5) on Python 3.12, in a CLI
session against an OpenAI-compatible provider.

## Install

The plugin is enabled per Hermes home and stays off until you enable it.

**Managed Hermes installs** (the `install.sh` installer, Hermes Desktop): install
the directory plugin in `hermes-plugin/futureagi/`. Hermes' package manager
installs `traceAI-hermes-agent` from its `pyproject.toml` when you enable it.

```bash
hermes plugins install future-agi/traceAI/python/frameworks/hermes-agent/hermes-plugin/futureagi
hermes plugins enable futureagi
```

**Hermes in a Python environment you manage** (a source checkout, a container
image): install the package into the environment Hermes runs in. It declares the
`hermes_agent.plugins` entry point `futureagi`.

```bash
pip install traceAI-hermes-agent
hermes plugins enable futureagi
```

## Configure

Put the keys in `$HERMES_HOME/.env` (normally `~/.hermes/.env`) or in the
environment Hermes runs in:

```bash
FI_API_KEY=your-futureagi-api-key
FI_SECRET_KEY=your-futureagi-secret-key
FI_PROJECT_NAME=hermes-agent
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | none | Required. Without them the plugin logs one warning and does nothing. |
| `FI_BASE_URL` | `https://api.futureagi.com` | Spans go to `$FI_BASE_URL/tracer/v1/traces`. |
| `FI_PROJECT_NAME` | `hermes-agent` | Future AGI project. |
| `FI_HERMES_CAPTURE_CONTENT` | off | `true` also exports prompts, responses, tool arguments and tool results. |
| `FI_HERMES_MAX_CHARS` | `8000` | Per-attribute cap on captured text. |

Restart Hermes (and its gateway, if you run one) after enabling the plugin.

## What is captured

| Hermes hook | Span | Fields |
| --- | --- | --- |
| `pre_llm_call` | `hermes.turn` start | `session.id`, `hermes.turn_id`, `hermes.task_id`, `hermes.platform`, `hermes.model`, first-turn flag, parent session |
| `pre_api_request` | `chat <model>` start | `gen_ai.request.model`, `gen_ai.provider.name`, `server.address` (host only), API mode, attempt and retry counts, message and tool counts, approximate input tokens |
| `post_api_request` | `chat <model>` end | `gen_ai.response.model`, finish reason, `gen_ai.usage.input_tokens` (includes cache), `output_tokens`, `total_tokens`, cache read/write tokens, reasoning tokens, time to first chunk |
| `api_request_error` | `chat <model>`, status `ERROR` | error category (`rate_limit`, `timeout`, ...), HTTP status, retryable flag |
| `pre_tool_call` / `post_tool_call` | span named after the tool | `gen_ai.tool.name`, `gen_ai.tool.call.id`, status, duration; `ERROR` when Hermes reports `error`, `blocked`, `timeout` or `cancelled` |
| `on_session_end` | `hermes.turn` end | completed / failed / interrupted, exit reason; `ERROR` when the turn failed |
| `subagent_start` | | parents the child session's turn under the parent turn |
| `on_session_finalize`, process exit | | closes spans that are still open (marked `hermes.unfinished`) and flushes |

Span timing for model calls uses the `started_at` / `ended_at` Hermes reports.

## Privacy

By default the plugin reads no text from the hook payloads: no prompt, response,
conversation history, tool argument, tool result, or provider error message.
Error statuses carry Hermes' error category, not the provider's text. The provider
base URL is reduced to its host.

With `FI_HERMES_CAPTURE_CONTENT=true`:

- the turn gets the user message as `input.value` and the final answer as `output.value`;
- each model call gets Hermes' sanitized request payload (`request`) as input and the assistant text or requested tool calls as output;
- each tool call gets its arguments and result;
- error statuses carry the provider or tool error text.

Each value is truncated to `FI_HERMES_MAX_CHARS`.

The plugin never reads Hermes' files: no session transcripts, `config.yaml`,
memory or skills. Everything it exports comes from the hook payloads.

## Not this plugin

- **Hermes' Langfuse plugin** sends to Langfuse's API, which Future AGI does not ingest.
- **Gateway health export** (`monitoring.export.otlp`) reports gateway health and delivery, not turns.

## Tests

```bash
pip install -e . pytest
pytest tests
```

With `hermes-agent` installed in the same environment, the contract tests also
check the hook names against `hermes_cli.plugins.VALID_HOOKS` and that Hermes
discovers the entry point.
