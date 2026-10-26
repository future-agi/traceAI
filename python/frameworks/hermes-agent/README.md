# traceAI-hermes-agent

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that exports
Hermes turns, model calls and tool calls to Future AGI as OpenTelemetry spans.
It registers observer callbacks on Hermes' documented plugin hooks; it is not
part of Hermes and does not change what Hermes does.

```
hermes.turn            AGENT  one user message to final answer (session, platform, outcome)
├── chat gpt-6-luna    LLM    one provider attempt: model, provider, tokens, finish reason
├── read_file          TOOL   one tool call: name, call id, duration, status
└── chat gpt-6-luna    LLM
```

One trace per top-level Hermes turn. Every span carries `session.id` (the Hermes
session id), so the turns of a `hermes chat` session group into one Future AGI
session. A subagent's turn sits inside its parent turn's trace and carries the
subagent's own session id.

Alpha. Tested with Hermes Agent v2026.9.24 (0.21.5) on Python 3.12, in CLI
sessions against an OpenAI-compatible provider. Supports Python 3.11 to 3.14;
the test suite also passes on Python 3.14 against Hermes v0.22.0, which runs on 3.14.

## Install

The plugin is enabled per Hermes home and stays off until you enable it.

**Managed Hermes installs** (the `install.sh` installer, Hermes Desktop): install
the directory plugin in `hermes-plugin/futureagi/`. `hermes plugins install`
installs `traceAI-hermes-agent` from its `pyproject.toml` into Hermes'
environment (and `enable` retries it if that failed).

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

To try an unreleased change, install from a traceAI checkout instead:
`pip install /path/to/traceAI/python/frameworks/hermes-agent`.

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
| `FI_HERMES_CAPTURE_CONTENT` | off | `true` also exports prompts, responses, tool arguments and results, and error text. |
| `FI_HERMES_MAX_CHARS` | `8000` | Per-attribute cap on captured text. |

In a multi-profile gateway (`gateway.multiplex_profiles`), each profile's turns
use that profile's own `.env` values, read through Hermes' profile secret scope.
A profile without keys is not traced; it never borrows another profile's keys.

Restart Hermes (and its gateway, if you run one) after enabling the plugin.

## What is captured

| Hermes hook | Span | Fields |
| --- | --- | --- |
| `pre_llm_call` | `hermes.turn` start | `session.id`, `hermes.turn_id`, `hermes.task_id`, `hermes.platform`, `hermes.model`, first-turn flag, parent session |
| `pre_api_request` | `chat <model>` start | `gen_ai.request.model`, `gen_ai.provider.name`, `server.address` (host only), API mode, attempt and retry counts, message and tool counts, approximate input tokens |
| `post_api_request` | `chat <model>` end | `gen_ai.response.model`, finish reason, `gen_ai.usage.input_tokens` (includes cache), `output_tokens`, `total_tokens`, cache read/write tokens, reasoning tokens, time to first chunk |
| `api_request_error` | `chat <model>`, status `ERROR` | Hermes' error category (`rate_limit`, `overloaded`, `timeout`, ...) as `error.type` and status, the error class as `hermes.error_class`, HTTP status, retryable flag |
| `post_tool_call` | span named after the tool | `gen_ai.tool.name`, `gen_ai.tool.call.id`, status, duration (the span is timed by Hermes' `duration_ms`); `ERROR` with Hermes' error type when the tool reports `error`, `blocked`, `timeout` or `cancelled` |
| `on_session_end` | `hermes.turn` end | completed / failed / interrupted, exit reason category; `ERROR` when the turn failed, otherwise `OK` |
| `subagent_start` | | parents the child session's turn under the parent turn |
| `on_session_finalize`, process exit | | closes spans that are still open (marked `hermes.unfinished`) and flushes |

Each provider attempt is its own span. The first attempt starts at the
`started_at` Hermes reports; a retry starts when its own `pre_api_request`
fires, so a failed attempt and its retry do not overlap.

The plugin does not register on `pre_tool_call`: Hermes treats that hook as a
fail-closed policy hook (a slow or failing callback blocks the tool), and its
callbacks never return a directive. A tool still running when Hermes exits
therefore has no span; a tool cancelled with Ctrl+C is reported, with status
`cancelled`.

## What is not captured

- Cost. The hooks carry token counts, not Hermes' cost estimate.
- Auxiliary model calls (titling, compression, vision, approvals) and streaming deltas.
- Gateway delivery, cron and kanban events, which are not agent turns.
- Gateway (messaging platform) sessions were not run in testing; they use the same hooks.

## Privacy

Always exported (metadata): session, turn and task ids, platform, model id,
provider name, the provider's host (from the base URL, without path or
credentials), tool names and call ids, token and message counts, durations,
statuses and Hermes' error categories. For a self-hosted provider the host can
be an internal hostname and the model id a local file name.

Not exported unless `FI_HERMES_CAPTURE_CONTENT=true`: the user message, the
assistant's answer, conversation history, the request sent to the model, tool
arguments, tool results, and all error text. Hermes puts exception text into
some turn exit reasons (`repeated_outer_errors(Error during API call #3: ...)`);
by default only the part before the parenthesis is exported.

With `FI_HERMES_CAPTURE_CONTENT=true`:

- the turn gets the user message as `input.value` and the final answer as `output.value`;
- each model call gets the latest messages of the request as input (newest kept
  when over the cap; the system prompt is reported by size only, as
  `system_prompt_chars`) and the assistant text or requested tool calls as output;
- each tool call gets its arguments and result; arguments can contain local paths;
- exit reasons and error statuses carry the full error text.

Each value is truncated to `FI_HERMES_MAX_CHARS`.

The plugin never reads Hermes' files: no session transcripts, `config.yaml`,
memory or skills. It installs no signal handlers, does not set the global
OpenTelemetry provider, and sends uncompressed OTLP/HTTP (the Future AGI
collector does not read `Content-Encoding`). When Future AGI is unreachable,
Hermes' exit and session teardown wait a few seconds for the export, then drop
those spans: about 3 seconds with current OpenTelemetry releases, at most about
6 with the oldest supported one (1.35).

## Not this plugin

- **Hermes' Langfuse plugin** sends to Langfuse's API, which Future AGI does not ingest.
- **Gateway health export** (`monitoring.export.otlp`) reports gateway health and delivery, not turns.
- **NeMo Relay file sinks** (ATOF / ATIF) write local files.
- **NeMo Relay's OpenTelemetry exporter**, which Hermes core drives, can also
  send OTLP to Future AGI (a `plugins.toml` with `header_env` for the two keys and
  `resource_attributes.project_name`). Tested with Hermes v2026.9.24 and
  nemo-relay 0.8.4 (`gen_ai` projection): one trace per session with turn, model
  and tool spans, but the model spans carry the full prompt and response
  (`gen_ai.input.messages` / `gen_ai.output.messages`) even with
  `enable_full_payloads = false`, no `session.id` is set, and the configuration is
  process-wide, so every profile of a gateway exports to one project. This plugin
  exists for a content-off default, Future AGI sessions and per-profile keys.

## Troubleshooting

- `hermes plugins list` must show `futureagi` as enabled in the Hermes home you use.
- The missing-keys warning and setup failures are logged by `traceai_hermes_agent`
  to `$HERMES_HOME/logs/agent.log` and `errors.log`.
- A gateway loads plugins and `.env` at start: restart it after changes.

## Tests

```bash
pip install -e . pytest
pytest tests
```

With `hermes-agent` installed in the same environment, the contract tests also
check the hook names against `hermes_cli.plugins.VALID_HOOKS` and that Hermes
discovers the entry point.
