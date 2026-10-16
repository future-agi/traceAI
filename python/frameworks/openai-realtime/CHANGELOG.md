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
- Alpha until a session against the live OpenAI Realtime API has been traced. Tested against a local
  stand-in server with openai 3.31.0, 3.24.0 and 2.0.0.
