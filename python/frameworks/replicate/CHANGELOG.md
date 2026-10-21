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
- **Stream spans carry no status, `predict_time` or token counts by default.** Replicate's event stream reports
  none, and `stream_reload_seconds` defaults to `0`: a stream span ends at the stream's `done` event (or the end of
  iteration), on the caller's thread, inside the `next()` / `__anext__()` that returns it, OK, with the prediction
  id, the output and what the stream says (`replicate.stream.done_reason`: a `done` with reason `canceled` records
  status `canceled`; `replicate.stream.truncated` for a stream that ended without `done`). No request is made after
  a stream, no thread is ever started, and `force_flush()`, `shutdown()`, `uninstrument()`, interpreter exit and
  `os.fork()` wait for nothing. `replicate.run` and create + `wait()` spans have status and token counts either way.
- Opt-in, `stream_reload_seconds` (up to 10): the prediction is read once after `done` (`GET /v1/predictions/{id}`:
  no new prediction and no charge, but it counts toward the account's API rate limit; twice when the API still
  answers a non-final status) for the final status, `predict_time` and token counts, within that budget. Its limits:
  - It goes only through a connection pool the package owns (on a background event loop, with the client's base URL
    and headers): when replicate built the client's transport (its proxy options are copied) or the transport for the
    API URL is a plain `httpx.HTTPTransport` / `AsyncHTTPTransport` without a proxy (its TLS context, HTTP versions,
    local address, socket options and connect retries are copied). Any other transport the caller passes (custom,
    mock, scripted, recording or logging wrapper, subclass, proxied, async-only) is never called for it: no read,
    `replicate.stream.read` = `skipped`, `replicate.stream.skip_reason` = `custom_transport`.
  - Completing a stream is one queue put on the caller's thread; the worker threads start in `instrument()` (a forked
    child starts its own on its first completed stream). Past 64 waiting stream spans (or 64 live reads) a stream ends
    at once on the caller's thread without a read (`at_capacity`), with an inline exporter including its export.
  - The budget is an absolute deadline that closes the read's connection. Name lookups run on at most 2 threads with
    at most 64 queued; reads of one name share a lookup, results are kept 5 s, and a lookup waits for a free thread
    within the budget. The read is never retried, asks for an uncompressed body, caps the body at 2 MiB as it
    arrives, and runs with instrumentation suppressed (no HTTP span).
  - `force_flush()`, `shutdown()`, interpreter exit and `uninstrument()` do not wait for reads in flight: each ends
    their spans at once with what is known (`replicate.stream.read` = `flushed`, `shutdown`, `shutdown`,
    `uninstrumented`), so they are exported (a `shutdown()` right after `uninstrument()` still exports them); the
    one span the finisher thread may be exporting then has ended, and that export is waited for (at most 5 s). With
    an inline exporter, ending those spans exports them inside that call, so exit still takes the export time of the
    spans waiting (at most 64). `stream_flush_wait_seconds` (0 to 10, default 0) opts into a wait in `force_flush()`,
    never more than half of `timeout_millis`, after which the provider's own flush gets the time left.
    `stream_shutdown_wait_seconds` (0 to 10, default 0) opts into a wait in `shutdown()` and at exit.
  - A fork waits (at most 5 s) for the package's finisher thread to be between two span ends; that is the only
    thread the package coordinates with a fork. Span processors' and exporters' behaviour in a forked child is
    theirs.
  - `replicate.stream.read`: `skipped`, `timeout`, `failed`, `unfinished`, `flushed`, `shutdown`, `uninstrumented`.
    `replicate.stream.skip_reason`: `custom_transport`, `at_capacity`, `no_provider_hook`, `after_uninstrument`,
    `after_shutdown`, `client_closed`, `no_prediction_id`, `start_failed`, `interrupted`. `aborted` is a final status.
- A stream's span starts at its first `next()`; a stream never iterated sends no request and has no
  span. A call under the caller's suppress-instrumentation context is not traced. A forked child never
  ends or exports a span its parent holds.
- Breaking out of a stream at `done` and then closing or dropping it is a completed stream (OK, with
  its output, and the read when it is on), not a cancellation.
- The placeholder version `hidden` that official models report is not recorded.
