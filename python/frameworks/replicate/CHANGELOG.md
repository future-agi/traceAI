# Changelog

All notable changes to `traceAI-replicate` will be documented here.

## [0.1.0] - Unreleased

### Added
- **Stream spans carry no status, `predict_time` or token counts by default.** Replicate's event stream reports
  none, and `stream_reload_seconds` defaults to `0`: a stream span ends at the stream's `done` event (or the end of
  iteration), on the caller's thread, inside the `next()` / `__anext__()` that returns it, OK, with the prediction
  id, the output and what the stream says (`replicate.stream.done_reason`: a `done` with reason `canceled` records
  status `canceled`; `replicate.stream.truncated` for a stream that ended without `done`). No request is made after
  a stream and no thread is ever started, so `force_flush()`, `shutdown()`, `uninstrument()`, interpreter exit and
  `os.fork()` wait for no read and no package thread (a fork ends no span); `force_flush()`, `shutdown()`,
  `uninstrument()` and exit still end, inside the call, held create spans and the spans of streams dropped since the
  last traced call (with an exporter that runs inline, exported there, also by `force_flush(timeout_millis=0)`).
  `replicate.run` and create + `wait()` spans have status and token counts either way.
- A traced call still running (`run`, `async_run`, a create, `wait()` or `cancel()`), or a referenced stream or run
  iterator still open, at provider shutdown (including application-owned SIGTERM/SIGINT cleanup that calls
  `provider.shutdown()`) or normal interpreter exit ends then as cancelled (ERROR `cancelled`, `replicate.cancelled`), with what it knows, so it is
  exported before the processors stop. Before, an in-flight `run()` / `async_run()` or create span was lost there, and
  a running `wait()` ended OK with its last polled status.
- A stream or run iterator that stops before it completes (closed, dropped, a cancelled task, Ctrl-C, an exception,
  or still open at shutdown or exit) keeps the output it had yielded (`output.value`, subject to `TraceConfig`), so a
  text stream stays `LLM`; its status is unchanged (ERROR `cancelled` with `replicate.cancelled` when the caller
  stopped it, ERROR with the error otherwise), as the OpenTelemetry GenAI conventions ask for partial streamed
  output.
- The caller stopping a call is recorded the same way wherever it lands (ERROR `cancelled`, `replicate.cancelled`,
  no exception event): a `KeyboardInterrupt` or `SystemExit` while `run()`, `async_run()`, a create, `wait()` or
  `cancel()` runs or inside a stream's `next()` / `__anext__()` (as one raised in the loop body is), a cancelled
  asyncio task or trio cancel scope (`trio.Cancelled`, also what anyio raises under trio), and
  `throw(GeneratorExit)`. Other libraries' cancellations are recorded as errors. A `close()` the generator refuses
  because another thread is inside its `next()` ends nothing; that stream ends its span.
- Opt-in, `stream_reload_seconds` (above 0 to 10): the prediction is looked up once after `done`
  (`GET /v1/predictions/{id}`: no new prediction and no charge, but it counts toward the account's API rate limit;
  a non-final answer is looked up once more after `poll_interval`) for the final status, `predict_time` and token
  counts, within that budget. Its limits:
  - It goes only through a connection pool the package owns (on a background event loop, with the client's base URL
    and headers), and only when the API-URL transport is a plain `httpx.HTTPTransport` / `AsyncHTTPTransport` on
    httpcore's default network backend, under replicate's own `RetryTransport` layers (exactly that class) if any.
    This includes replicate's default, a plain transport the caller passes, or a plain transport mounted with
    `mounts=` (TLS context, HTTP versions, local address, socket options and connect retries are copied). Any other
    transport (custom, mock, scripted, recording or logging wrapper, a subclass, a transport or pool with
    `handle_request` / `handle_async_request` set on the instance, custom network backend or backend with
    `connect_tcp` / `connect_unix_socket` set on the instance, an instance-level `_transport_for_url` or one a subclass
    of `httpx.Client` / `AsyncClient` overrides (never called for the read), `HTTPXClientInstrumentor.instrument_client()`
    on the client, a proxy or `proxy=`, async-only) is skipped: `replicate.stream.read=skipped`,
    `replicate.stream.skip_reason=custom_transport`.
  - No read while a checked `httpx` / `httpcore` client, transport, pool, connection or network-backend send path is
    replaced on the class (respx, pytest-httpx, VCR.py, `mock.patch`, Sentry's `HttpxIntegration`): such a mock could
    answer the package's request with a response the app scripted for itself (`patched_http`). It also skips when a
    checked socket layer is replaced (`socket.socket`, `socket.getaddrinfo`, `socket.create_connection`,
    `ssl.SSLContext` or its `wrap_socket` / `wrap_bio`: mocket, httpretty, gevent, eventlet and
    `truststore.inject_into_ssl()`; `patched_socket`). Both checks run at stream completion and again before each
    request. `opentelemetry-instrumentation-httpx` wrappers are allowed. A stub server (pytest-httpserver, WireMock,
    Prism) is a real server and receives the read like any request to `base_url`: keep `stream_reload_seconds=0` in
    tests that use one.
  - Completing a stream runs the read's checks and admission on the caller's thread (no I/O) and one queue put; the
    worker threads start in `instrument()` (a forked
    child starts its own on its first completed stream). Past 64 waiting stream spans (or 64 live reads) a stream ends
    at once on the caller's thread without a read (`at_capacity`); an inline export of that span is not bounded by
    the read's deadline.
  - The budget is an absolute deadline that closes the read's connection. Name lookups run on at most 2 threads with
    at most 64 queued; reads of one name share a lookup, results are kept 5 s, and a lookup waits for a free thread
    within the budget. The read is never retried, asks for an uncompressed body, caps the body at 2 MiB as it
    arrives, and runs with instrumentation suppressed (no HTTP span).
  - `force_flush()`, `shutdown()`, interpreter exit and `uninstrument()` do not wait for reads in flight by default:
    each ends their spans at once with what is known (`replicate.stream.read` = `flushed`, `shutdown`, `shutdown`,
    `uninstrumented`) and cancels reads in flight (closing their connections). `uninstrument()` never waits for a
    read; like a flush, shutdown or exit, it waits up to 5 s for the one span the finisher is exporting (that span
    has ended; the wait is not repeated after a timeout until the finisher ends another span, and `timeout_millis`
    does not shorten it). An inline exporter can still make the call export spans synchronously, up to 64 times its
    per-span export time. `stream_flush_wait_seconds` (0 to 10, default 0) opts into a bounded wait for reads in
    `force_flush()`, never more than half of `timeout_millis`, after which the provider's own flush gets the time
    left. `stream_shutdown_wait_seconds` (0 to 10, default 0) opts into a bounded wait in `shutdown()` and also at
    interpreter exit; no wait option applies to `uninstrument()`.
  - Before starting workers, `instrument()` imports AnyIO's lazy asyncio backend on the caller's thread. At a fork,
    the package pauses its read loop between steps and finisher between span ends, giving the fork up to 5 s total
    to wait for one export; after the cap a child can inherit an exporter lock held mid-export, and later forks do
    not wait for that thread again until it has moved on (so a stuck export costs one fork the wait, not every
    fork). The dispatcher and resolver threads are not paused. A `BatchSpanProcessor` and exporters are not coordinated; their fork safety
    is theirs, not this package's.
  - `replicate.stream.read`: `skipped`, `timeout`, `failed`, `unfinished`, `flushed`, `shutdown`, `uninstrumented`.
    `replicate.stream.skip_reason`: `custom_transport`, `patched_http`, `patched_socket`, `at_capacity`, `no_provider_hook`,
    `after_uninstrument`, `after_shutdown`, `client_closed`, `no_prediction_id`, `start_failed`, `interrupted`.
    `aborted` is a final status.
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
- A stream's span starts at its first `next()`; a stream never iterated sends no request and has no
  span. A call under the caller's suppress-instrumentation context is not traced. A forked child never
  ends or exports a span its parent holds.
- Breaking out of a stream at `done` and then closing or dropping it is a completed stream (OK, with
  its output, and the read when it is on), not a cancellation. `send()` / `asend()` step a stream as
  `next()` / `__anext__()` do, and `throw()` / `athrow()` end its span with that error (as cancelled for
  `GeneratorExit`, `KeyboardInterrupt`, `SystemExit` and an asyncio or trio cancellation). A
  `KeyboardInterrupt` while a stream's span is completed at `done` still ends the span.
- Output text keeps every chunk in order, including chunks that start like a URL (`https://`,
  `http://`, `data:`, or the `FileOutput` replicate 1.0.7 makes of them); output is file URLs (and a
  stream's `replicate.stream.file_count`) only when every chunk is a URL or a file.
- The placeholder version `hidden` that official models report is not recorded.
