# Changelog

## 0.1.0 (alpha)

- New package: tracing for Gemini Live sessions opened with `client.aio.live.connect` on the
  Gemini Developer API (google-genai `>=2.28,<3`; tested on 2.31.0 and 2.28.0 with
  `gemini-3.8-live`). Gemini Live is a preview API: a field rename upstream is a floor and
  fixture update here, not a silent fallback. google-genai 3.0 removes `Live.send` /
  `Live.start_stream`, which this package never wrapped; pin below 3.
- One trace per session: setup, user turns, model turns (model, tokens, transcription), tool
  calls with cancellation, interruptions, `goAway`, and a resume with the stored handle as a
  `gemini.live.reconnect` span in the same trace.
- Audio sessions carry `fi.span.kind=CONVERSATION` on a parentless root plus `call.*` and
  `conversation.transcript*`, so Future AGI lists them as calls. Text-only sessions are `CHAIN`.
- Privacy: audio bytes, API keys and resumption handles never land on a span. Text content is
  on by default; `metadata_only=True` (or `TraceConfig` / `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS`)
  drops it. Raw error text is content; stack traces are never exported.
- Not supported: Vertex AI Live, ephemeral tokens, the deprecated `send` / `start_stream`,
  LiveKit / Pipecat sessions, Node.
