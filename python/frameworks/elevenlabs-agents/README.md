# traceAI-elevenlabs-agents

Forward finished [ElevenLabs Agents](https://elevenlabs.io/agents) conversations to
Future AGI Observe. Each conversation becomes one trace and one row in the Voice tab,
with its turns, tool calls, the LLM model and token counts ElevenLabs reports, and
(when you opt in) the transcript.

ElevenLabs runs the agent, so there is nothing to instrument. After a call ends and
analysis completes, ElevenLabs POSTs an OpenTelemetry JSON trace to a webhook URL you
host. This package is that webhook: it runs in your environment, checks the
`ElevenLabs-Signature` header, maps the trace onto Future AGI's attribute names and
exports it to Future AGI with your own keys. Future AGI never sees your ElevenLabs
signing secret.

Status: alpha. Verified end to end against a local Future AGI collector with a
documented-shape ElevenLabs stand-in (see "What was tested"), not yet against a live
ElevenLabs workspace.

## Install

```bash
pip install traceAI-elevenlabs-agents
```

Python 3.10 to 3.13. Standard library only at runtime (no ElevenLabs SDK, no OpenTelemetry SDK).

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
| `FI_ELEVENLABS_CAPTURE_CONTENT` | off | `true` also exports transcript text, tool arguments and results, reasoning, analysis, dynamic variables, the user id, status messages and span event attributes |
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

With `FI_ELEVENLABS_CAPTURE_CONTENT=true`, also: `conversation.transcript` and
`fi.conversation.transcript` (the same JSON list of `{role, content, time}` turns),
`conversation.transcript.{i}.message.role` / `.content`, `input.value` / `output.value`
on turns (an LLM turn's input is the user turn before it), tool `input.value` /
`output.value` from `elevenlabs.tool.params` / `elevenlabs.tool.result`, `user.id`
(only when the GET backfill response carries `user_id`), status messages, event
attributes and every other attribute ElevenLabs sent.

With content capture off (the default) only identifiers, timings, counts, status codes,
model names and token numbers are exported: no transcript text, tool arguments or
results, reasoning, RAG queries, analysis results, dynamic variables, user ids, status
messages or event attributes, and none of the payload's other resource attributes.

Always dropped: signed media URLs (they expire in about 15 minutes; no recordings are
stored) and any `project_name` / `fi.*` resource attribute in the payload.

## Not captured

- `call.talk_ratio` and per-turn audio durations: the OpenTelemetry payload has no
  per-side audio durations, so they are omitted rather than guessed.
- Recordings (`post_call_audio`), the monitoring WebSocket (Enterprise-gated, a
  different span shape), TTS-only API calls, RAG chunk text (not in the trace).
- A live in-progress view: a conversation appears after ElevenLabs finishes analysis.

## What was tested

- Unit and contract tests (`pytest tests`): 74 tests, Python 3.10, 3.12, 3.13. The HMAC
  check is tested against a hand-computed HMAC-SHA256 and against the real
  `elevenlabs` SDK `construct_event` (2.73.0, the latest stable on 2026-10-15, and
  2.70.0).
- `examples/run_e2e.sh`: the real `traceai-elevenlabs-agents` process against a local
  Future AGI collector, with ElevenLabs stood in by `examples/standin.py` (docs-shaped
  deliveries signed with a local secret; a GET endpoint the real SDK client parses).
- Fixtures follow the documented payload and the attribute names in the Langfuse
  ElevenLabs cookbook. They are not captured from a live workspace (see
  `tests/fixtures/README.md`).

## Sources (re-read 2026-10-15)

- https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces
- https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks
- https://langfuse.com/integrations/no-code/elevenlabs (attribute names, "fresh span ids on every export")
