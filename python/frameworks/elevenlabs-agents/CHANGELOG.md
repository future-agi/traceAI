## [0.1.0] - unreleased
### Feature
- ElevenLabs Agents post-call OpenTelemetry forwarder: HMAC-verified `post_call_transcription_otel`
  webhook receiver, Voice-tab mapping (`fi.span.kind=CONVERSATION` root, `call.duration`,
  `call.total_turns`, LLM model and tokens, tool spans, transcript), per-conversation
  idempotency record, and a one-shot `backfill` command for `GET ?format=opentelemetry`.
- Content on by default, like every traceAI instrumentor; the standard `FI_HIDE_INPUTS` /
  `FI_HIDE_OUTPUTS` (and the `*_MESSAGES` / `*_TEXT` variants) or `TraceConfig(...)` drop it,
  and `FI_PII_REDACTION` works as elsewhere. No package-specific privacy switch.
- Never exported in any mode: resource attributes outside an allowlist (no host, process or
  OS details), environment and identity span attributes, stack traces, and emails, local
  paths and private host names in metadata and error text.
### Fixed
- A live webhook body carries the caller id only as the root span attribute `elevenlabs.user_id`
  (there is no `data.user_id`), so `user.id` was missing on webhook deliveries. The root attribute is
  now used when `data.user_id` is absent.
### Verified
- 2026-10-19: live `post_call_transcription_otel` webhook deliveries (HMAC, ok / abrupt disconnect /
  2-minute timeout / with user id). Captured bodies added as sanitized fixtures.
- `backfill` run against the live ElevenLabs API (2026-10-17); attribute names matched. Added a
  sanitized captured body as a test fixture.
