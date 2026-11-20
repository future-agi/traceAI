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

## Install

```bash
pip install traceAI-elevenlabs-agents
```

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
| `ELEVENLABS_API_KEY` | for `backfill` or RAG enrichment | API key with conversation read access; enrichment also needs read access to the referenced knowledge-base documents |
| `FI_ELEVENLABS_API_BASE_URL` | `https://api.elevenlabs.io` | ElevenLabs API host for `backfill` and RAG enrichment (for example a data-residency host) |
| `FI_ELEVENLABS_ENRICH_RAG` | `false` | Explicitly enable extra API reads to attach retrieved documents; see [RAG enrichment](#rag-enrichment) |

### Backfill one conversation

```bash
ELEVENLABS_API_KEY=... traceai-elevenlabs-agents backfill --conversation-id conv_...
```

Calls `GET /v1/convai/conversations/{id}?format=opentelemetry` once and forwards the
result through the same mapper. A conversation the webhook already forwarded is
acknowledged as `duplicate` and not exported again: ElevenLabs keeps the trace id but
mints new span ids on every export, so a second export would duplicate every turn and
tool span. `--force` exports anyway; use it only when the first export never landed.

### RAG enrichment

To inspect retrieved documents alongside an agent's answer, explicitly enable extra
ElevenLabs API reads before starting the receiver or running `backfill`:

```bash
export ELEVENLABS_API_KEY="..." # conversation and knowledge-base read access
export FI_ELEVENLABS_ENRICH_RAG=true
traceai-elevenlabs-agents --host 0.0.0.0 --port 8787
```

For a RAG-enabled turn, the forwarder reads the regular conversation JSON and the
referenced knowledge-base chunks. It matches the native turn index, retrieval query
and embedding model. Documents appear on that **existing agent turn** as
`retrieval.documents.{i}.document.content`, `.id` and `.metadata`; the agent response
remains `output.value`. No span is added, retyped or retimed. The native
`elevenlabs.rag.retrieval_query`, `.embedding_model` and `.latency_ms` remain available.

Document metadata identifies `source=elevenlabs.knowledge_base_api` and
`snapshot=current`: the chunk API returns the current stored chunk, not a guaranteed
snapshot of what the agent saw when the call ran. It also preserves the document ID,
embedding model and reported vector distance. Do not treat that distance as a relevance
score or the retrieved text as an immutable historical record.

Extra reads are **off by default** and are also skipped when any input- or output-side
`FI_HIDE_*` content switch is on. Enrichment uses the same PII, identity and stack-trace
redaction policies as the forwarder. Duplicate deliveries cause no extra reads. Each
conversation examines at most 16 chunk references, caches repeated document/chunk/model
lookups, and exports at most 4 KiB of UTF-8 content per chunk. Each API response is capped
at 4 MiB and each request has a 2-second socket timeout. Redirects are not followed.

The turn's `elevenlabs.rag.enrichment.status` is `complete`, `limited` (reference budget),
`partial` (one or more chunks unavailable), `unmatched` (no reliable turn match), or
`unavailable` (conversation could not be read). `content_truncated` in each document's
metadata indicates the content limit. Enrichment failures do not block the native trace
or trigger another delivery: check permissions and the status when documents are
missing. Do not use `--force` just to enrich an already exported call; it duplicates
native spans.

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
  different span shape), TTS-only API calls, historical RAG chunk snapshots.
- RAG chunk text unless you enable [RAG enrichment](#rag-enrichment).
- A live in-progress view: a conversation appears after ElevenLabs finishes analysis.

## References

- https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces
- https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks
- https://langfuse.com/integrations/no-code/elevenlabs (attribute names, "fresh span ids on every export")
