# traceAI-elevenlabs-agents

Forward finished [ElevenLabs Agents](https://elevenlabs.io/agents) conversations to
Future AGI Observe. Each conversation becomes one trace and one row in the Voice tab,
with its transcript, turns, tool calls, and the LLM model and token counts ElevenLabs
reports. Content follows the standard traceAI `FI_HIDE_*` switches.

ElevenLabs runs the agent, so there is nothing to instrument. After a call ends and
analysis completes, ElevenLabs POSTs an OpenTelemetry JSON trace to a webhook URL you
host. This package is that webhook: it runs in your environment, checks the
`ElevenLabs-Signature` header, maps the trace onto Future AGI's attribute names and
exports it to Future AGI with your own keys. Future AGI never sees your ElevenLabs
signing secret.

Status: alpha. The `backfill` path (the `GET ...?format=opentelemetry` export) was verified
on 2026-10-17 against a live ElevenLabs workspace and a local Future AGI. Webhook delivery
was verified on 2026-10-19: a real ElevenLabs workspace POSTed signed
`post_call_transcription_otel` deliveries to this forwarder on Cloud Run, and the traces
were stored in Future AGI cloud. Not seen live: ElevenLabs retries, a duplicate delivery,
a span with status ERROR, and a call longer than 2 minutes.

## Install

Not on PyPI yet. Until it is released, install from this branch:

```bash
pip install "git+https://github.com/future-agi/traceAI.git@feat/th-8231-elevenlabs-agents#subdirectory=python/frameworks/elevenlabs-agents"
```

After the release: `pip install traceAI-elevenlabs-agents`.

Python 3.10 to 3.13. One runtime dependency, `fi-instrumentation-otel`, for
`TraceConfig` (the privacy switch every traceAI package shares). No ElevenLabs SDK.

## Run

```bash
export ELEVENLABS_WEBHOOK_SECRET="..."   # the secret ElevenLabs shows when you create the workspace webhook
export FI_API_KEY="..."
export FI_SECRET_KEY="..."
export FI_PROJECT_NAME="elevenlabs-calls"
traceai-elevenlabs-agents --host 0.0.0.0 --port 8787
```

The webhook path is `/elevenlabs/webhook` (`--path` to change it); `GET /healthz`
answers 200. Put HTTPS in front of it (your ingress or a reverse proxy) and register
`https://<your-host>/elevenlabs/webhook` in ElevenLabs:

1. ElevenAgents dashboard: create a workspace webhook with that URL (HMAC authentication).
2. [Agents settings](https://elevenlabs.io/app/agents/settings): assign it as the
   post-call webhook, enable the **Transcript** event and **OpenTelemetry transcript
   payloads** (`transcript_format: opentelemetry`). Turn on **Enable retries** so
   ElevenLabs retries 5xx responses.

| Variable | Default | Meaning |
|---|---|---|
| `ELEVENLABS_WEBHOOK_SECRET` | required for `serve` | HMAC secret of the workspace webhook |
| `FI_API_KEY`, `FI_SECRET_KEY` | required | Future AGI keys |
| `FI_PROJECT_NAME` | required | Observe project; created on first export if it does not exist. A payload cannot choose another project |
| `FI_BASE_URL` | `https://api.futureagi.com` | Future AGI base URL; traces go to `{FI_BASE_URL}/tracer/v1/traces` |
| `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | `false` | The standard traceAI switches; see [Privacy](#privacy) |
| `FI_PII_REDACTION` | `false` | The standard traceAI switch: replace emails, phone numbers, card numbers ... inside the exported content |
| `FI_ELEVENLABS_STATE_PATH` | `~/.traceai-elevenlabs-agents/forwarded.sqlite3` | Idempotency record (one row per forwarded conversation). Keep it on a persistent volume |
| `FI_ELEVENLABS_SIGNATURE_TOLERANCE_SECS` | `1800` | Replay window, the 30 minutes the ElevenLabs SDK uses |
| `FI_ELEVENLABS_PAUSED` | off | `true` answers 503 to every delivery (ElevenLabs retries later) |
| `ELEVENLABS_API_KEY` | only for `backfill` | API key with `CONVAI_READ` |
| `FI_ELEVENLABS_API_BASE_URL` | `https://api.elevenlabs.io` | ElevenLabs API host for `backfill` (for example a data-residency host) |

### Backfill one conversation

```bash
ELEVENLABS_API_KEY=... traceai-elevenlabs-agents backfill --conversation-id conv_...
```

Calls `GET /v1/convai/conversations/{id}?format=opentelemetry` once and forwards the
result through the same mapper. A conversation the webhook already forwarded is
acknowledged as `duplicate` and not exported again: ElevenLabs keeps the trace id but
mints new span ids on every export, so a second export would duplicate every turn and
tool span. `--force` exports anyway; use it only when the first export never landed.

## Responses

| Delivery | HTTP | Stored |
|---|---|---|
| Missing, malformed, stale or wrong signature | 401 | nothing; the body is not parsed |
| Declared body over 20 MiB | 413 | nothing; the body is not read |
| `post_call_transcription`, `post_call_audio`, `call_initiation_failure` | 200 `ignored` | nothing |
| No `resourceSpans`, no conversation id, malformed span ids | 400 + code | nothing |
| Conversation already forwarded (retry) | 200 `duplicate` | nothing |
| Same conversation being exported by another worker | 503 | nothing |
| Future AGI unreachable or refused the batch | 502 | nothing; ElevenLabs may retry |
| Accepted | 200 `forwarded` | one trace |

The 2xx is sent only after Future AGI accepted the batch.

## What is captured

Kept as ElevenLabs sent it: trace id, span ids, parent ids, span names (unknown names
pass through), timestamps, status codes.

| Span | Added attributes |
|---|---|
| `elevenlabs.conversation` (root) | `fi.span.kind=CONVERSATION`, `gen_ai.system=elevenlabs`, `session.id` (conversation id), `call.duration` (root span seconds, else `time_in_call_secs`), `call.total_turns` (user + agent turn spans), `elevenlabs.agent_id`, `elevenlabs.source`, `elevenlabs.cost_fiat` (total ElevenLabs cost incl. TTS/ASR, not LLM cost) |
| `elevenlabs.recv.agent_response` with `elevenlabs.producing_llm` | `fi.span.kind=LLM`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens` from `elevenlabs.llm_usage.<model>.input` / `.output_total` |
| other agent and user turns | `fi.span.kind=CHAIN` |
| `elevenlabs.tool.{name}` | `fi.span.kind=TOOL`, `tool.name` |
| every span | `session.id` |

The conversation content, captured by default like every traceAI instrumentor:
`conversation.transcript` and `fi.conversation.transcript` (the same JSON list of
`{role, content, time}` turns), `conversation.transcript.{i}.message.role` / `.content`,
`input.value` / `output.value` on the root (first user turn, last agent turn), on turns
(an LLM turn's input is the user turn before it) and on tools (from
`elevenlabs.tool.params` / `elevenlabs.tool.result`), `user.id` when ElevenLabs supplies
one, the other `elevenlabs.*` attributes ElevenLabs sent, status messages and
exception messages.

## Privacy

Content follows `fi_instrumentation.TraceConfig`, the switch every traceAI instrumentor
uses, read from the standard environment variables at startup (or pass
`ForwarderConfig(..., trace_config=TraceConfig(...))`). There is no package-specific
privacy setting.

| Switch | Drops |
|---|---|
| `FI_HIDE_INPUTS`, `FI_HIDE_INPUT_MESSAGES` or `FI_HIDE_INPUT_TEXT` = `true` | What goes in: user turn text, tool arguments, RAG queries, dynamic variables, `user.id`, every `input.value` |
| `FI_HIDE_OUTPUTS`, `FI_HIDE_OUTPUT_MESSAGES` or `FI_HIDE_OUTPUT_TEXT` = `true` | What comes out: agent turn text, tool results, reasoning, analysis and data-collection results, status and exception messages, every `output.value` |
| either side | The combined transcript, and any attribute that cannot be placed on one side (for example `elevenlabs.event.data`) |
| `FI_PII_REDACTION=true` | Emails, phone numbers, card numbers, IP addresses ... inside what is still exported |

In a conversation the turns are the messages, so the `*_MESSAGES` and `*_TEXT`
variants hide a side the same way `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` do. With all of
them on, only identifiers, timings, counts, status codes, model names and token numbers
are exported, and the call is still listed in the Voice tab with its duration and turns.

Never exported, whatever the switches say:

- resource attributes outside an allowlist (`service.*`, `deployment.environment`,
  `telemetry.sdk.*`, the ElevenLabs conversation id, agent id, environment and source):
  no `host.*`, `process.*`, `os.*` or anything else about the machine;
- span attributes under `host.`, `process.`, `os.`, `user.`, `enduser.`, `code.`,
  `client.`, `cloud.`, `container.`, `k8s.`, stack-trace keys and credential-named keys;
- stack traces: error text is cut to one line (the exception line of a traceback), and
  a stack trace inside a tool result is cut the same way;
- email addresses, local file paths and private host names (`*.local`, `*.internal`,
  `*.lan` ...) in metadata (dynamic variables, analysis results, unknown attributes,
  status and exception messages): replaced with `<EMAIL_ADDRESS>`, `<FILE_PATH>`,
  `<HOSTNAME>`; a `user.id` that contains one is not written;
- signed media URLs (they expire in about 15 minutes; no recordings are stored) and
  any `project_name` / `fi.*` resource attribute in the payload;
- the ElevenLabs signing secret, the `ElevenLabs-Signature` header, the ElevenLabs API
  key and the Future AGI keys: never on a span, in a response or in a log line. Log
  lines carry the conversation id and the outcome only.

What a caller says is conversation content: an email address spoken in a turn stays in
the transcript unless you set `FI_PII_REDACTION=true` or hide that side.

## Not captured

- `call.talk_ratio` and per-turn audio durations: the OpenTelemetry payload has no
  per-side audio durations, so they are omitted rather than guessed.
- Recordings (`post_call_audio`), the monitoring WebSocket (Enterprise-gated, a
  different span shape), TTS-only API calls, RAG chunk text (not in the trace).
- A live in-progress view: a conversation appears after ElevenLabs finishes analysis.

## What was tested

- Unit and contract tests (`pytest tests`): 112 tests on Python 3.10, 3.12 and 3.13 with
  `fi-instrumentation-otel` 1.1.0 (latest), and on 0.1.11 (the lowest allowed; the
  `FI_PII_REDACTION` test is skipped there because that release has no such switch). The
  HMAC check is tested against a hand-computed HMAC-SHA256 and against the real
  `elevenlabs` SDK `construct_event` (2.73.0, the latest stable on 2026-10-15, and
  2.70.0).
- `examples/run_e2e.sh`: the real `traceai-elevenlabs-agents` process against a local
  Future AGI collector, with ElevenLabs stood in by `examples/standin.py` (docs-shaped
  deliveries signed with a local secret; a GET endpoint the real SDK client parses).
  The webhook leg was also run live on 2026-10-19 (see Live below); this stand-in is for the failure cases a live workspace does not produce.
- Live, 2026-10-17: a throwaway agent and three short text-only SDK conversations
  (`elevenlabs` 2.73.0), exported with `backfill` against the real ElevenLabs API into a
  local Future AGI (`fi_verify` PASS, privacy scan clean with `FI_HIDE_*` on). The real
  attribute names matched. ElevenLabs sent status OK for failing tool calls (404, an
  unanswered client tool), so no live ERROR span was produced; the ERROR mapping is
  covered by fixtures only. `tests/fixtures/live_get_conversation_opentelemetry.json` is
  one sanitized captured body, used by `tests/test_live_capture.py`.
- The other fixtures follow the documented payload and the attribute names in the Langfuse
  ElevenLabs cookbook (see `tests/fixtures/README.md`).

## Sources (re-read 2026-10-15)

- https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces
- https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks
- https://langfuse.com/integrations/no-code/elevenlabs (attribute names, "fresh span ids on every export")
