## [0.1.0] - 2026-10-15
### Feature
- ElevenLabs Agents post-call OpenTelemetry forwarder: HMAC-verified `post_call_transcription_otel`
  webhook receiver, Voice-tab mapping (`fi.span.kind=CONVERSATION` root, `call.duration`,
  `call.total_turns`, LLM model and tokens, tool spans), content capture off by default
  (`FI_ELEVENLABS_CAPTURE_CONTENT`), per-conversation idempotency record, and a one-shot
  `backfill` command for `GET ?format=opentelemetry`.
