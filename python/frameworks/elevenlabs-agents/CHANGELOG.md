## [0.1.1] - 2026-11-19
### Changed
- A content key is masked by key whatever the type of its value (number, boolean or text), including `input.value`, `gen_ai.input.images` and `gen_ai.request.parameters`.

## [0.1.2](https://github.com/future-agi/traceAI/compare/traceai-elevenlabs-agents-v0.1.1...traceai-elevenlabs-agents-v0.1.2) (2026-11-28)


### Features

* **fi-instrumentation:** add a shared conversation transcript policy ([88a8469](https://github.com/future-agi/traceAI/commit/88a846977f3404fe1f982a89f13da15f51ed4021))
* **fi-instrumentation:** add a shared conversation transcript policy ([e5f04d7](https://github.com/future-agi/traceAI/commit/e5f04d770d675dd42e5edb16b97217332b4d0184))


### Bug Fixes

* **fi-instrumentation:** keep the transcript policy out of package imports ([18bf7da](https://github.com/future-agi/traceAI/commit/18bf7da827f9d038db9e1eb5321dae4a7bc0900f))

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
