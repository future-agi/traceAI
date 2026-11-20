# traceAI-google-genai-live

Future AGI tracing for **Gemini Live** sessions that your Python process opens itself with
the official SDK: `client.aio.live.connect(...)` on the Gemini Developer API.

One Live session is one trace. Inside it you get the setup, each user turn, each model turn
(model, tokens, transcription), each tool call, interruptions, cancellations, `goAway`, and
a reconnect that resumes the session with the handle this process stored. Audio sessions are
listed as calls in Future AGI's voice views.

Status: **alpha**. Gemini Live is a preview API.

| Requirement | Version |
|---|---|
| google-genai | `>=2.28,<3`; use `>=2.34,<3` for in-session context updates |
| Live model | A [Live-capable model](https://ai.google.dev/gemini-api/docs/live-api/capabilities) on the Gemini Developer API |
| Python | 3.10 - 3.13 (`fi-instrumentation-otel` declares `<3.14`) |

## Install

```bash
pip install traceAI-google-genai-live "google-genai>=2.28,<3"
```

## Use

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google import genai
from traceai_google_genai_live import instrument

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="my-voice-agent")
instrument(tracer_provider=trace_provider)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
async with client.aio.live.connect(model="gemini-3.8-live",
                                   config={"response_modalities": ["AUDIO"],
                                           "input_audio_transcription": {},
                                           "output_audio_transcription": {}}) as session:
    ...  # your send_realtime_input / send_client_content / receive / send_tool_response loop
```

`GoogleGenAILiveInstrumentor().instrument(tracer_provider=...)` is the same thing.
A complete runnable example with a tool call is in [`examples/voice_tool.py`](examples/voice_tool.py).

Options:

| Option | Default | Effect |
|---|---|---|
| `config=TraceConfig(...)` or the standard `FI_HIDE_*` environment variables | content on | `hide_inputs` / `FI_HIDE_INPUTS` drops the input side (system instruction, user text and transcription, tool arguments); `hide_outputs` / `FI_HIDE_OUTPUTS` drops the output side (model text and transcription, tool results). `*_input_messages` / `*_input_text` (and the output pair) do the same, because all Live content is message text. Both sides off leaves metadata only: model, voice name, timings, tool names, flags, close codes and token counts. The conversation transcript and raw error text need both sides on. |
| `session_id`, `user_id` | none | Defaults for every session. `fi_instrumentation.using_session(...)` / `using_user(...)` around `connect` override them per call. |
| `resume_window_seconds` | `30` | How long a session whose connection ended after `goAway`, or died, waits for a reconnect with its stored handle before the root span closes. |

## What you get

```
gemini.live.session        CONVERSATION (audio) / CHAIN (text-only); parentless when CONVERSATION
├─ gemini.live.setup       url.path (no host, no query), system instruction (content)
├─ gemini.live.context_update  ordered instruction/tool updates; send status and safe metadata
├─ gemini.live.user_turn   transcription or text (content); gemini.turn_start / gemini.turn_boundary
├─ gemini.live.model_turn  LLM: model, llm.token_count.*, gemini.usage.*, transcription (content)
│  └─ gemini.live.tool     tool name, call id, args and result (content); cancelled / unanswered flags
├─ gemini.live.reconnect   gap between the dropped connection and the resumed setup
└─ gemini.live.tool_response   a response for a cancelled (ignored) or unknown (unmatched, ERROR) call id
```

Root attributes: `gen_ai.system=google_genai`, `gen_ai.request.model`, `session.id`
(+ `gemini.session_id_source` = `customer` | `server` | `local`), `user.id` only if you set
one, `gemini.response_modalities`, `gemini.voice_name`, `gemini.tool_names`,
`gemini.realtime_input_count`, `gemini.go_away`, `gemini.time_left`, `gemini.reconnect_count`.

For audio sessions the root also carries the voice-call fields: `call.duration`,
`call.total_turns` (omitted, not 0, when no turn closed), `call.talk_ratio` (model speech
seconds / user speech seconds; omitted unless both were measured: user speech from the
server's `voiceActivity` offsets, model speech from raw PCM length at the declared rate), and
`conversation.transcript` / `fi.conversation.transcript` plus flattened
`conversation.transcript.{i}.message.role|content` rows when content capture is on.

How server events map:

- `generationComplete` sets `gemini.generation_complete` and does not close the model turn.
  `turnComplete` closes it. `interrupted` closes it at once (`gemini.interrupted=true`).
- `send_client_content` during an open model turn closes that turn with
  `gemini.close_reason=client_content`.
- A user turn opens on the server's `voiceActivity` `ACTIVITY_START` (or your
  `activity_start`, a realtime `text=` input, or the first input transcription) and closes on
  `ACTIVITY_END` (`gemini.turn_boundary=activity_end`), falling back to `turnComplete`
  (`gemini.turn_boundary=turn_complete`). `send_client_content` is its own user turn.
- `toolCall` opens a model turn if none is open (real sessions send the call with no model
  output before it, then `turnComplete` + usage for that generation), and one tool span per
  function call. The span closes on your `send_tool_response` with the same id.
  `toolCallCancellation` closes it with `gemini.cancelled=true` and no result.
- `usageMetadata` is copied onto the open model turn (latest message wins on that span, and
  every message is also a span event). Nothing is summed. In real `gemini-3.8-live` sessions
  each message covered one generation (its prompt count includes the context so far).
- Audio frames sent with `send_realtime_input` are counted, never spanned.

## Update instructions or tools during a session

With `google-genai>=2.34,<3`, use the SDK's `send_context_update` inside your
existing `async with client.aio.live.connect(...)` block:

```python
await session.send_context_update(system_instruction="Keep answers concise.")
await session.send_context_update(tools=[])  # remove all tools for subsequent responses
```

Each call produces a `gemini.live.context_update` child span. The span records a
monotonic `gemini.context_update.sequence` in call order, which fields were supplied,
and `gemini.context_update.sent`. A successful send means the SDK returned normally,
not that Gemini acknowledged applying the change. Await updates in the order you want
Gemini to receive them; instrumentation does not serialize or retry concurrent sends.

Instruction text follows the input privacy controls. Tool names remain metadata;
tool descriptions, schemas and media are not captured. Omitted fields (or `None`)
leave their previous state unchanged; `tools=[]` explicitly clears the tool names.
Errors and cancellations end the update span without advancing context state and
reach your code unchanged.

Each newly opened model turn records the latest successfully sent instruction and
tool revisions in `gemini.context.system_instruction_revision` and
`gemini.context.tools_revision` (`0` means the initial setup), together with the
current tool names. Revisions refer to the update sequence, not a server-side
acknowledgement. An update does not become a user turn or enter the conversation
transcript. Existing turns keep their opening snapshot, and resumed connections
keep the session's update sequence. Older SDKs without this method retain all
other session tracing.

## Failures

| Case | Trace |
|---|---|
| Setup rejected (bad model, bad key) | One ERROR root (`setup_rejected`) with the setup span; the SDK's `APIError` reaches your code unchanged. No turns, no `call.total_turns`. |
| Socket dies (no `goAway`) | Open spans close ERROR `socket_closed`, `gemini.close_code` on the root, one flush. No reconnect by this package. |
| `goAway` | `gemini.go_away` / `gemini.time_left` on the root; the root stays open. |
| Reconnect with the stored handle | Same trace, one `gemini.live.reconnect` span. Failure: root ERROR `resume_failed`. |
| Reconnect with a handle this process did not store | New trace (`gemini.resumed_unknown_handle=true`). |
| Task cancelled | Open spans and the root close ERROR `task_cancelled`. |
| Exception from `receive()` | Recorded on the root, re-raised unchanged. |
| Export queue full | Spans are dropped by the batch processor; the socket reader is never blocked. |

## Privacy

- Default: text on, audio off, as in every traceAI instrumentor. Audio bytes (`realtimeInput`
  and `modelTurn` inline data) never land on a span.
- Metadata only: `FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true` (or
  `TraceConfig(hide_inputs=True, hide_outputs=True)`). There is no package-specific switch.
- The API key is never copied. The connection URL is recorded as its path only.
- The session resumption handle stays in memory and is never a span attribute or a log line.
- Raw error text is content: it is recorded only when both content directions are on. Stack
  traces are never exported.

## Not supported

- Vertex AI Live (`vertexai=True`): one warning, not traced.
- Ephemeral tokens (`auth_tokens/...`, `BidiGenerateContentConstrained`): one warning, not traced.
- The deprecated `session.send` and `session.start_stream` are not wrapped. Use
  `send_client_content`, `send_realtime_input`, `send_tool_response` and `receive`.
- `generate_content` / `generate_content_stream`: use `traceAI-google-genai`.
- Gemini Live inside LiveKit or Pipecat: use `traceAI-livekit` / `traceAI-pipecat`.
- Node `@google/genai`.
- google-genai 3.x (the version check refuses it).
- No audio recording attachment, no cost in currency (the platform estimates cost from model
  and tokens where it has a price).

## Tests

```bash
pip install -e . pytest pytest-asyncio opentelemetry-exporter-otlp-proto-http
pytest
```

The tests run the real google-genai SDK against a scripted socket (`tests/conftest.py`).
`tests/fixtures/real_20261015_*.json` are server frames recorded from real
`gemini-3.8-live` sessions (audio replaced by silence, resumption handles replaced).
`tests/_voice_tab.py` is a local stand-in for the shared TH-8339 voice-tab assertion helper,
which had not merged; replace it with the shared helper when it lands.
