## [0.1.0] - 2026-10-15
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
