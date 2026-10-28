# Examples

| File | What it is |
|---|---|
| `voice_agent.py` | A server-side voice agent on `client.realtime.connect()`, traced to Future AGI: session setup with input transcription and server VAD, a `get_weather` tool, playback accounting and barge-in with `conversation.item.truncate`. `--typed "..."` asks a typed question and gets a spoken answer (no microphone), `--text` runs a text-only session, `--error-case` sends two invalid client events (voice and `--typed` sessions). |
| `realtime_stub_server.py` | A **stand-in** for the OpenAI Realtime WebSocket. It speaks the documented GA event vocabulary with scripted transcripts, tool calls and token counts, so the example and the tests run with no OpenAI key. It is not OpenAI. |
| `run_e2e.sh` | Every scenario, in a fresh virtualenv, against a Future AGI collector: live when `OPENAI_API_KEY` is set, against the stand-in otherwise. |

## Run

```bash
export FI_API_KEY=... FI_SECRET_KEY=...       # Future AGI (FI_BASE_URL too for a self-hosted collector)
export OPENAI_API_KEY=...                      # leave unset to use the stand-in
PYTHON=python3.12 examples/run_e2e.sh my-voice-agent 3.33.0
# live runs: OPENAI_REALTIME_MODEL=gpt-realtime-mini keeps the cost to cents
```

Against the stand-in it runs six sessions: a spoken call with a tool, barge-in and two server
errors; the same call with `FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true`; a typed question; a
text-only session; a socket drop mid-response; and a refused connection. Against the live API it
runs the typed question with a tool call and two invalid client events (with and without content
capture), a text-only session and a refused connection (the live API closes the socket with code
3000 `invalid_api_key`; the span ends `socket_closed`), because the synthetic microphone cannot script a barge-in on a real model.

Each run prints two markers. The first is sent with content capture on and shows up in the traces.
The second is sent with `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` on and must not appear anywhere in the
project.

To run the agent alone:

```bash
python examples/realtime_stub_server.py --port 8765 &      # stand-in, or skip it and use OpenAI
OPENAI_API_KEY=sk-local OPENAI_WEBSOCKET_BASE_URL=ws://127.0.0.1:8765/v1 \
  python examples/voice_agent.py --project my-voice-agent
```

`OPENAI_WEBSOCKET_BASE_URL` is the example's own setting; it is passed to
`AsyncOpenAI(websocket_base_url=...)`.
