# Changelog

All notable changes to `traceAI-replicate` will be documented here.

## [0.1.0] - Unreleased

### Added
- `ReplicateInstrumentor` for the `replicate` client (`replicate>=1.0.0,<2`): `run`, `stream`, every
  prediction create route, and `wait` / `cancel`, sync and async, one span per user call.
- A create followed by `wait()` or `cancel()` is one span; a create nobody waits on ends with its
  create-time status, bounded by `max_pending_seconds` and the provider's flush and shutdown.
- Model, version, prediction id and status, `predict_time`, input and output (following `TraceConfig`),
  and the token counts a language model's prediction reports, as `gen_ai.usage.input_tokens` /
  `output_tokens` / `total_tokens`. No cost is recorded.
- The API token is removed from every recorded value, status and exception; `pii_redaction` also covers
  the error status and the exception event.
