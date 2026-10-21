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
  `predict_time` and token counts, within `stream_reload_seconds` (default 2, at most 10, `0` turns it
  off). Completing a stream is a queue put: the caller's loop never waits, does no I/O and starts no
  thread for it. No read is made for a span that is not recording, after the provider is shut down,
  or where no flush/shutdown hook could be installed.
- The read goes through a connection pool the package owns (16 connections, on a background event
  loop, with the client's base URL, headers and proxy options) when replicate built the client's
  transport; the budget is then an absolute deadline that closes the read's connection, and name
  lookups run on at most 2 threads. Through a transport the caller passed it runs on at most 4 reader
  threads (or, for an async-only transport, on the caller's loop), and never on an `httpx` pool of fewer
  than 8 connections. At most 64 reads are live at once, counted until their I/O returns. It is never
  retried by the package, asks for an uncompressed body, caps the body at 2 MiB as it arrives, and runs
  with instrumentation suppressed (no HTTP span).
- `force_flush(timeout_millis)` waits for reads in flight at most half its timeout, then ends their spans
  with what is known and gives the provider's flush the rest, so it never blocks longer than asked;
  `shutdown()` and exit wait at most the budget; `uninstrument()` does not wait.
- Stream spans record `replicate.stream.read` (`skipped`, `timeout`, `failed`, `unfinished`) when the
  read gave no final status, `replicate.stream.done_reason` (a `done` with reason `canceled` records
  status `canceled`), and `replicate.stream.truncated` for a stream that ended without `done`.
  `aborted` is a final status.
- A stream's span starts at its first `next()`; a stream never iterated sends no request and has no
  span. A call under the caller's suppress-instrumentation context is not traced. A forked child never
  ends or exports a span its parent holds.
- Breaking out of a stream at `done` and then closing or dropping it is a completed stream (OK, with
  its output and the read), not a cancellation.
- The placeholder version `hidden` that official models report is not recorded.
