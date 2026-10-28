# traceAI-openai-realtime

OpenTelemetry tracing for OpenAI Realtime sessions that your own Python process opens with
`client.realtime.connect()`. One connection is one trace. An audio session shows up as a call in
Future AGI's Voice tab; a text-only session shows up in Traces.

> **Live-verified (2026-10-17), still alpha.** Run against OpenAI's live Realtime API
> (`gpt-realtime-mini`, `openai` 3.33.0): session, typed question with a spoken answer, a function
> call, token usage from `response.done`, server `error` events, a text-only session, a refused API
> key and `FI_HIDE_*`. Still tested only against the local stand-in: barge-in and
> `conversation.item.truncate`, real microphone audio (`call.talk_ratio`), a 1011 socket drop
> mid-response, and `openai` 3.24.0 / 2.0.0.

## Install

```bash
pip install traceAI-openai-realtime "openai[realtime]>=2.0.0,<4"
```

Python 3.10 to 3.13. `openai[realtime]` brings `websockets`, which the SDK needs to open the socket.

## Use

Call `register()` first, then instrument. Your Realtime code does not change.

```python
import asyncio

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import AsyncOpenAI
from traceai_openai_realtime import OpenAIRealtimeInstrumentor

# FI_API_KEY and FI_SECRET_KEY in the environment, plus OPENAI_API_KEY.
provider = register(project_type=ProjectType.OBSERVE, project_name="my-voice-agent")
OpenAIRealtimeInstrumentor().instrument(tracer_provider=provider)


async def main() -> None:
    client = AsyncOpenAI()
    async with client.realtime.connect(model="gpt-realtime-2.1") as connection:
        await connection.session.update(session={"type": "realtime", "instructions": "Be brief."})
        async for event in connection:
            ...  # your event loop: play audio, run tools, handle barge-in


asyncio.run(main())
```

The sync client (`OpenAI().realtime.connect()`) is traced the same way. So is a connection opened
with `.enter()` and closed with `.close()`, and one read with `recv()`, `recv_bytes()` or
`dispatch_events()`.

`instrument()` options:

| Option | Effect |
|---|---|
| `tracer_provider` | The provider from `register()`. |
| `config=TraceConfig(hide_inputs=True)` | Drop what goes in: user speech and text, instructions, tool arguments. Also `FI_HIDE_INPUTS=true`, `FI_HIDE_INPUT_MESSAGES=true` or `FI_HIDE_INPUT_TEXT=true`. |
| `config=TraceConfig(hide_outputs=True)` | Drop what comes out: assistant speech and text, tool results, error messages. Also `FI_HIDE_OUTPUTS=true`, `FI_HIDE_OUTPUT_MESSAGES=true` or `FI_HIDE_OUTPUT_TEXT=true`. |
| `session_id`, `user_id` | Written on every session root. `using_session()` / `using_user()` around `connect()` take precedence. |

`examples/voice_agent.py` is a complete server-side voice agent: session setup with input
transcription and server VAD, a tool, playback accounting and barge-in with
`conversation.item.truncate`. Its microphone is a synthetic tone, so against the live API use
`--typed "What's the weather in Paris?"`: a typed question, a tool call and a spoken answer, with no
microphone.

## What you get

```
openai.realtime.session         root, no parent. CONVERSATION for audio sessions, CHAIN for text-only
  openai.realtime.user_turn     one per user utterance (server VAD or push-to-talk commit)
  openai.realtime.response      one per model response (LLM): model, tokens, input and output
    openai.realtime.tool        one per function call the response made
  realtime.error                one per server `error` event
```

| Span | Attributes |
|---|---|
| session | `gen_ai.system=openai`, `gen_ai.request.model`, `session.id`, `user.id` (only if you set one), `realtime.openai_session_id`, `realtime.voice`, `realtime.output_modalities`, `realtime.end_reason`, `realtime.close_code`; for audio sessions `call.duration`, `call.total_turns`, `call.talk_ratio`, `conversation.transcript`, `fi.conversation.transcript`, `conversation.transcript.{i}.message.role/content`; `input.value` = the session instructions |
| user turn | `output.value` = the input transcription, `realtime.transcription` (`completed`, `failed`, `not_enabled`, `not_received`), `realtime.turn_detection`, `realtime.audio_start_ms`, `realtime.audio_end_ms`, `realtime.audio_duration_ms` |
| response | `gen_ai.request.model`, `gen_ai.response.id`, `gen_ai.usage.*` from `response.done` only, `input.value` (the conversation items added since the previous response), `output.value` (transcript or text), `llm.output_messages.0.message.tool_calls.*`, `realtime.response.status`, `realtime.response.status_reason`, `realtime.audio_delta_count`, `realtime.audio_duration_ms`; on barge-in `realtime.cancelled`, `realtime.audio_end_ms`, `realtime.truncation` |
| tool | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments` and `input.value`, `gen_ai.tool.call.result` and `output.value` |
| error | `error.type`, `error.code`, `realtime.error.param`, `error.message` |

Missing data is omitted, never zeroed: no usage in `response.done` means no token attributes, a call
that ends before any turn has no `call.total_turns`, and `call.talk_ratio` is written only when both
sides' audio durations are known. User speech duration comes from the server's VAD timestamps (or
the committed buffer's length for push-to-talk); assistant duration comes from the decoded length of
the audio deltas, capped at `audio_end_ms` when the client truncated. No currency cost is sent.

### Barge-in

The Realtime API reports a cancelled response as `response.done` with `status: "cancelled"` and a
`reason` of `turn_detected` or `client_cancelled`; there is no separate cancel event. The response
span closes then, with `realtime.cancelled=true` and status OK. If your client sends
`conversation.item.truncate`, the span records `realtime.audio_end_ms` (how much was heard) and
`realtime.truncation`: `server_confirmed` once `conversation.item.truncated` arrives,
`client_sent_unconfirmed` before that, or `not_sent` for a cancelled response with no truncate. The
stored transcript is the text the model generated before it stopped: the server does not send back a
truncated transcript.

## Privacy

- Audio bytes never land on a span, in any mode. Audio is only counted and measured.
- Content is on by default, like every traceAI instrumentor, and the switch is the same one:
  `FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true` (or `TraceConfig(hide_inputs=True, hide_outputs=True)`)
  turns it off. Names, timings, token counts, tool names, error types and codes, model and voice
  stay. In a Realtime session the transcripts are the messages, so `FI_HIDE_INPUT_MESSAGES` /
  `FI_HIDE_INPUT_TEXT` drop the user side the same way `FI_HIDE_INPUTS` does, and the `OUTPUT`
  variants drop the model side. Either side drops the combined `conversation.transcript`. There is no
  package-specific privacy switch.
- The API key is never recorded: not the `Authorization` header, not a key in a URL query, not the
  browser subprotocol form. Error and exception text is scrubbed of the client's key and of anything
  shaped like a key before it is written. URLs and hosts are not recorded at all.
- Exceptions are recorded by type, and by message only with output capture on. Stack traces are
  never recorded (they carry local paths).
- `OpenAI-Safety-Identifier` is not copied into `user.id`.

## Lifecycle, reconnects and errors

- The root opens when `connect()` is entered and closes on `close()`, on leaving the `async with`
  block, or when the socket closes. Sessions still open at interpreter exit are ended before the
  tracer provider flushes, as long as `register()` ran before `instrument()`.
- A socket that drops (an abnormal close) ends the open response and the root with status ERROR and
  `realtime.end_reason=socket_closed`, and flushes the exporter once. The instrumentor never opens a
  socket or retries one.
- The SDK (3.x) reconnects only when you pass `on_reconnecting` to `connect()`. Then a drop closes
  the spans in flight, records `realtime.socket_dropped`, and the root stays open: the same
  connection object stays one trace. A second `connect()` you write is a new trace; the Realtime API
  has no resume handle.
- An `error` event becomes a `realtime.error` child span; the session goes on.
- An exception out of your `async with` block is recorded on the root and re-raised unchanged.
  Cancelling the task ends the session with `realtime.end_reason=task_cancelled`.
- A refused connection (for example HTTP 401) is one `CHAIN` root with status ERROR and
  `realtime.end_reason=connect_failed`.
- Export never blocks the socket reader: spans go to the batch processor `register()` set up, which
  drops (and counts) spans when its queue is full rather than waiting.

## Not covered

- `client.live.connect()` (GPT-Live, `/v1/live/sessions`). A `connect()` whose websocket base URL
  points at `/live/...` logs one warning and runs untraced.
- WebRTC and browser sessions, `client.beta.realtime`, transcription-only sessions.
- LiveKit (`livekit.plugins.openai.realtime`) and Pipecat (`OpenAIRealtimeLLMService`): use
  traceAI-livekit and traceAI-pipecat. This package does not touch them, so nothing is counted twice.
- Node. Azure OpenAI Realtime clients are untested.
- Recordings: the instrumentor does not record or attach audio.

## Versions

| openai | Status |
|---|---|
| 3.31.0 | Tested (latest on 2026-10-15) |
| 3.24.0 | Tested |
| 2.0.0 | Tested (floor; `client.realtime.connect` first appears here) |

Earlier releases have no `client.realtime.connect` and are not supported. traceAI-openai (chat,
responses, embeddings) is a separate package and is not changed by this one.

## Running without an OpenAI key

`examples/realtime_stub_server.py` is a **stand-in** for the Realtime WebSocket: it speaks the
documented GA event vocabulary with scripted transcripts, tool calls and token counts, so the example
and the tests can run with no OpenAI account. It is not OpenAI.

```bash
python examples/realtime_stub_server.py --port 8765 &
OPENAI_API_KEY=sk-local OPENAI_WEBSOCKET_BASE_URL=ws://127.0.0.1:8765/v1 \
  python examples/voice_agent.py --project my-voice-agent
```

`OPENAI_WEBSOCKET_BASE_URL` is the example's own setting; it is passed to
`AsyncOpenAI(websocket_base_url=...)`.

## Tests

```bash
pip install -e . pytest
pytest tests
```

The tests replay recorded event sequences into the recorder, and run the real SDK (sync and async)
against the stand-in server on 127.0.0.1. `tests/voice_tab.py` holds a local copy of the Voice-tab
assertions until the shared helper exists.
