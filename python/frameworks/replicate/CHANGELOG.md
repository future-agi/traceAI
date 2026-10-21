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
  off). Completing a stream is one queue put on the caller's thread: it waits for nothing, does no I/O
  and starts no thread (the worker threads start in `instrument()`; a forked child starts its own on
  its first completed stream). The one exception is an async-only transport of the caller's, whose
  read runs as a task on the caller's loop. No read is made for a span that is not recording, after
  the provider is shut down, or where no flush/shutdown hook could be installed.
- The read goes through a connection pool the package owns (one connection per admitted read, on a
  background event loop, with the client's base URL and headers) when replicate built the client's
  transport (its proxy options are copied) or the caller passed a plain `httpx.HTTPTransport` /
  `AsyncHTTPTransport` without a proxy (its TLS context, HTTP versions, local address, socket options
  and connect retries are copied). The budget is then an absolute deadline that closes the read's
  connection. Name lookups run on at most 2 threads; reads of one name share a lookup, results are
  kept 5 s, and a lookup waits for a free thread within the budget. Through any other transport of
  the caller's it runs on a reader thread (or, for an async-only transport, as a task on the caller's
  loop, ended at once when that loop cancels it), at most 4 per transport and 16 in all, and never on
  an `httpx` pool the package can see that has fewer than 8 connections. At most 64 stream spans wait
  for their read at once (reads in flight, and spans waiting to be exported), counted until the read's
  I/O returns. It is never retried by the package, asks for an uncompressed body, caps the body at
  2 MiB (as it arrives, when the transport streams it), and runs with instrumentation suppressed (no
  HTTP span).
- `force_flush()` does not wait for reads in flight: their spans end at once with what is known
  (`replicate.stream.read` = `flushed`) and that flush exports them. `stream_flush_wait_seconds`
  (0 to 10, default 0) opts into a wait, never more than half of `timeout_millis`. The provider's own
  flush gets the time left, so with a batch span processor the call never takes longer than asked
  (with an inline exporter, the spans it ends are exported inside the call). `shutdown()` and exit
  wait at most the budget (plus, with an inline exporter, exporting at most 64 waiting spans);
  `uninstrument()` does not wait. A fork waits for a span export in progress on the package's thread.
- Stream spans record `replicate.stream.read` (`skipped`, `timeout`, `flushed`, `failed`, `unfinished`)
  when the read gave no final status, `replicate.stream.done_reason` (a `done` with reason `canceled`
  records status `canceled`), and `replicate.stream.truncated` for a stream that ended without `done`.
  `aborted` is a final status.
- A stream's span starts at its first `next()`; a stream never iterated sends no request and has no
  span. A call under the caller's suppress-instrumentation context is not traced. A forked child never
  ends or exports a span its parent holds.
- Breaking out of a stream at `done` and then closing or dropping it is a completed stream (OK, with
  its output and the read), not a cancellation.
- The placeholder version `hidden` that official models report is not recorded.
