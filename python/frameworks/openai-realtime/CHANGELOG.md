# Changelog

## 0.1.0 (alpha)

- New package. Traces `client.realtime.connect()` (sync and async) on the official `openai` SDK,
  `>=2.0.0,<4`: one trace per connection, with user turns, responses (model, tokens, input and
  output), tool calls, error events, barge-in (cancel and truncate) and socket drops.
- Audio sessions get a parentless `CONVERSATION` root with the Voice-tab fields (`call.duration`,
  `call.total_turns`, `call.talk_ratio`, transcript); text-only sessions get a `CHAIN` root.
- Content on by default, like every traceAI instrumentor; the standard `FI_HIDE_INPUTS` /
  `FI_HIDE_OUTPUTS` (and the `*_MESSAGES` / `*_TEXT` variants) or `TraceConfig(...)` drop it. Audio
  bytes, API keys, URLs and stack traces are never recorded.
- Live-verified 2026-10-17 against the OpenAI Realtime API (gpt-realtime-mini, openai 3.33.0): typed
  question with tool call and spoken answer, token usage, server error events, text-only session,
  refused key (the live API closes the socket with code 3000 `invalid_api_key`, so the root ends
  `socket_closed`), and `FI_HIDE_*`. Still alpha: barge-in/truncate, microphone audio and a 1011
  mid-response drop are verified only against a local stand-in server (openai 3.31.0, 3.24.0, 2.0.0).
