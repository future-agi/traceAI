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
- Exception events carry `exception.type` and `exception.message` only: no stack trace, so no local
  file path or user name leaves the process.
- A completed stream reads its prediction once after `done` (`GET /v1/predictions/{id}`: no new
  prediction and no charge, but it counts toward the account's API rate limit) for the final status,
  `predict_time` and token counts. The read runs on a background thread, so the caller's loop never
  waits for it; it has a wall-time budget of `stream_reload_seconds` (default 2, `0` turns it off), is
  never retried (no `RetryTransport`, no `Retry-After`), runs with instrumentation suppressed (no HTTP
  span), and at most 256 run at once. The span ends when the read returns or the budget runs out, as
  of the stream's end; `force_flush()`, `shutdown()`, `uninstrument()` and exit wait for it at most
  that long.
- Breaking out of a stream at `done` and then closing or dropping it is a completed stream (OK, with
  its output and the read), not a cancellation.
- The placeholder version `hidden` that official models report is not recorded.
