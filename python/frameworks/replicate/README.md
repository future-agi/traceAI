# traceAI-replicate

OpenTelemetry instrumentation for the official
[`replicate`](https://github.com/replicate/replicate-python) Python client.

Replicate's API is predictions, not OpenAI chat completions, so the OpenAI
instrumentor does not trace this client: pointing `openai.OpenAI` at
`api.replicate.com` traces nothing. This package wraps the Replicate client
itself. It does not set or rewrite the client's base URL.

**Stream spans carry no status, `predict_time` or token counts by default**
(the event stream reports none, and the package does not read the
prediction after it unless you opt in); see [streams](#streams-no-status-or-token-counts-by-default).

## Installation

```bash
pip install traceAI-replicate
```

It accepts `replicate>=1.0.0,<2` and is tested with `replicate` 1.0.0 and
1.0.7 on Python 3.10, 3.11, 3.12 and 3.13.

## Usage

```python
import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

tracer_provider = register(project_type=ProjectType.OBSERVE, project_name="my-project")
ReplicateInstrumentor().instrument(tracer_provider=tracer_provider)

output = replicate.run("owner/model", input={"prompt": "What is the capital of France?"})
```

The client reads `REPLICATE_API_TOKEN` itself; do not put the token in span
attributes. Instrument before you use the client. A function imported with
`from replicate import run` before `instrument()` keeps the untraced original;
`replicate.run`, `replicate.async_run`, `replicate.stream` and
`replicate.async_stream` looked up after `instrument()`, and every
`replicate.Client` method, are traced.

### Streams: no status or token counts by default

Replicate's event stream carries no status, `predict_time` or token counts.
**By default (`stream_reload_seconds=0`) this package does not fetch them**:
a `replicate.stream` span ends at the stream's `done` event, on your thread,
with status OK, the prediction id, the output and what the stream itself
said, and **without `replicate.prediction.status`, `predict_time` or
`gen_ai.usage.*`**. Nothing is requested after the stream, the package never
starts a thread, and `force_flush()`, `shutdown()`, `uninstrument()`,
interpreter exit and `fork()` wait for nothing (with an exporter that runs
inline, a flush or shutdown still exports, inside that call, the spans of
streams dropped since the last traced call). `replicate.run` and create +
`wait()` spans have status and token counts either way (the client polls
those predictions to the end itself).

To get them on stream spans, opt in to one read of the prediction after
`done`:

```python
ReplicateInstrumentor().instrument(tracer_provider=tracer_provider, stream_reload_seconds=2)
```

The opt-in has limits. Each read is one more API request against your rate
limit. It runs only through the package's own connection pool, for
replicate's default transport or a plain `httpx` transport without a proxy:
with any other transport you pass to `replicate.Client` (or a subclass, a
transport changed on the instance, or `proxy=`) there is no read
(`replicate.stream.skip_reason=custom_transport`), and there is none while an
HTTP mock such as respx, pytest-httpx or VCR.py has patched `httpx` or
`httpcore` (`patched_http`). Past 64 waiting stream spans, a stream ends
without one. A flush, shutdown, exit or `uninstrument()` does not wait for a
read in flight unless you opt in to that too: the span ends without the
read's data. The opt-in in its current form has been checked against a local
stand-in only, not the live API (see "Live vendor verification" below). See
[the opt-in read after a stream](#the-opt-in-read-after-a-stream).

### Options

`instrument()` accepts `tracer_provider`, `config` (a
`fi_instrumentation.TraceConfig`), `max_pending_seconds` (a positive
number, default `600`; see [create, wait and cancel](#create-wait-and-cancel)),
and three options for [the opt-in read after a
stream](#the-opt-in-read-after-a-stream), each from `0` to `10` seconds and
`0` by default: `stream_reload_seconds` (the read's budget; `0` = no read),
`stream_flush_wait_seconds` (how long `force_flush()` waits for reads in
flight) and `stream_shutdown_wait_seconds` (how long `shutdown()` and
interpreter exit wait for them). Any other option, a `config` that is not a
`TraceConfig`, or a `max_pending_seconds` or `stream_*` value that is not an
`int` or `float` raises `TypeError`; a `max_pending_seconds` that is not a
positive, finite number, or a `stream_*` value outside 0 to 10, raises
`ValueError`.

A call made while OpenTelemetry instrumentation is suppressed in the
caller's context (`_SUPPRESS_INSTRUMENTATION_KEY`) is not traced at all.

## Instrumented calls

| Call | Span |
|---|---|
| `Client.run`, `async_run` (and `replicate.run`) | One `replicate.run` span. The create request and the client's own polling run inside it and add no span. If the client returns an iterator, the span stays open until the iterator is exhausted, closed, or dropped. |
| `Client.stream`, `async_stream` (and `replicate.stream`) | One `replicate.stream` span for the whole stream, not one per event. The client's stream is lazy: it sends nothing until it is first iterated, so the span starts at the first `next()` (as a child of the span that was current when you called `stream()`; `send()` / `asend()` step it as `next()` does, and `throw()` / `athrow()` end it with that error), and a stream that is never iterated has no span. The stream is complete at its `done` event; breaking out of the loop there and closing or dropping the stream is a completed stream (OK, with its output), not a cancellation. By default the span ends there, inside the `next()` / `__anext__()` that returns `done`, without status, `predict_time` or token counts (the event stream reports none). With `stream_reload_seconds` > 0 the prediction is then read once more in the background; see [the opt-in read after a stream](#the-opt-in-read-after-a-stream). A stream that raises, or is closed or dropped before `done`, makes no read. |
| `predictions.create`, `async_create`, also `models.predictions.create` and `deployments.predictions.create` | One `replicate.predictions.create` span. See [create, wait and cancel](#create-wait-and-cancel). |
| `Prediction.wait`, `async_wait` | Ends the open create span of that prediction, or opens one `replicate.prediction.wait` span. A `wait()` on a prediction that is already finished makes no request and adds no span. |
| `Prediction.cancel`, `async_cancel`, `predictions.cancel(id)`, `async_cancel(id)` | Ends the open create span of that prediction, or opens one `replicate.predictions.cancel` span. |
| `trainings.*`, `predictions.get` / `list`, `reload`, file uploads | Not traced. A training call produces no span from this package. |

Each span is a child of the active span when there is one. It is current
while the client sends its requests, so spans from an HTTP client
instrumentation nest under it. The opt-in read after a stream is the
exception: it runs with OpenTelemetry instrumentation suppressed and adds no
span.

### The opt-in read after a stream

**Off by default.** With `stream_reload_seconds=0` (the default) none of
this section applies: a stream span ends at `done` with what the stream gave
(no status, `predict_time` or token counts), no request is made after the
stream and no thread is started. A `done` whose data says
`{"reason": "canceled"}` still records status `canceled`.

**On.** With `stream_reload_seconds` from `0` (exclusive) to `10`, after
`done` the package reads the prediction once (`GET /v1/predictions/{id}`)
and records its final status, `predict_time` and token counts on the stream
span. Its limits:

- **Cost and rate limit.** The read creates no prediction and is not billed,
  but it is an API request: it counts toward your account's rate limit for
  requests other than creates (3000 per minute by
  [Replicate's rate-limit page](https://replicate.com/docs/topics/predictions/rate-limits)),
  one per stream, two when the API still answers `processing` for a
  prediction that has just sent `done`.
- **Only through the package's own connection pool.** The read uses a pool
  this package owns, on a background event loop, with your client's base URL
  and headers, and only when the transport your client uses for the API URL
  is replicate's own `RetryTransport` (exactly that class, not a subclass)
  over exactly a plain `httpx.HTTPTransport` / `httpx.AsyncHTTPTransport`
  without a proxy, with nothing set on either instance. That is the
  transport `replicate.Client` builds itself (the package's pool then gets
  the same client options), or a plain one you pass, such as
  `transport=httpx.AsyncHTTPTransport(retries=2)` (its TLS context, HTTP
  versions, local address, socket options and connect retries are copied).
  **Any other transport is never called for the read**: a custom, mock or
  scripted transport, a recording, logging or auth wrapper, a subclass of an
  `httpx` transport or of replicate's `RetryTransport`, a transport (or its
  pool) whose `handle_request` / `handle_async_request` was set on the
  instance (`mock.patch.object`), a transport with a proxy or a client with
  `proxy=`, or one that is only async. The stream span then has no read
  data, with `replicate.stream.read` = `skipped` and
  `replicate.stream.skip_reason` = `custom_transport`. So the read never
  runs on your thread or event loop, never calls your transport and never
  takes a connection from your pool.
- **Not through an HTTP mock.** A test double that replaces `httpx`'s or
  `httpcore`'s send methods on the class (respx, pytest-httpx, VCR.py,
  `mock.patch("httpx.AsyncClient.send")`) would see the package's own
  request and could answer it with a response your test scripted for your
  own code. While any of `httpx.Client` / `AsyncClient` `send`,
  `_send_handling_auth`, `_send_handling_redirects`, `_send_single_request`
  and `_transport_for_url`, `HTTPTransport.handle_request`,
  `AsyncHTTPTransport.handle_async_request`, and `httpcore`'s
  `ConnectionPool` / `HTTPConnection` `handle_request` and their async
  twins is not the library's own function, no read is made
  (`replicate.stream.skip_reason` = `patched_http`). It is checked when the
  stream completes and again just before the request is sent, so a mock
  started while a read is already being sent can still see that one read.
  opentelemetry-instrumentation-httpx's wrappers are the one change allowed
  (they add no span for the read). Another library that replaces those
  methods itself, as some APM agents' `httpx` integrations do, also turns
  the read off.
- **Only for a span that is exported.** No read is made for a span that is
  not recording (a trace your sampler dropped, `NoOpTracerProvider`), after
  the provider is shut down, or when no flush/shutdown hook could be
  installed on the provider (see [which provider](#which-provider)).
- **Off your path, until the cap.** Completing a stream is one queue put on
  your thread: your `next()` / `__anext__()` does not wait for the read and
  does no I/O for it. The package's worker threads (a read loop, a
  dispatcher and a finisher) start in `instrument()`, and up to 2 name
  resolver threads on the first host-name lookup; a forked child starts its
  own on its first completed stream. **Past the cap** (64 stream spans
  already waiting, or 64 reads live) a stream that completes ends at once
  **on your thread**, without a read (`skipped`, `at_capacity`); with an
  exporter that runs inline (`SimpleSpanProcessor`, `register(batch=False)`)
  that end includes its export, as it would with the read off. The read's
  own work still runs in your process and competes for the GIL. With 200
  async streams finishing at once against a stand-in API in another process,
  with replicate's default transport, on a shared machine (load average 26
  to 31), the median time from `done` to the end of each loop was 31.5 to
  56.3 ms with the read on and one shared client, and 48.5 to 190.2 ms with a
  client per stream, against 17.9 to 29.3 ms with it off and 13.2 to 20.5 ms
  uninstrumented, over 3 and 2 runs (code `f1866eb`). That cost is CPU time, so it grows with
  CPU contention.
- **Bounded.** `stream_reload_seconds` is a wall-time budget from the
  stream's end. The read is one request, and one more `poll_interval` later
  only when the prediction is not finished yet. It is never retried: a `429`
  or `5xx` ends it, and `Retry-After` is not waited on. The budget is an
  absolute deadline: when it runs out the request is cancelled and its
  connection closed, in whatever phase it is (name lookup, connect, headers,
  body). Name lookups run on at most 2 background threads with at most 64
  lookups queued; reads that need the same name share one lookup, a result is
  kept for 5 s, and a lookup waits for a free thread within the read's budget
  (it fails at once, `failed`, only while both threads have been stuck on a
  lookup for 5 s, or while 64 lookups are queued). The read asks for an
  uncompressed body; a compressed one is inflated in bounded steps, and the
  body is limited to 2 MiB as it arrives.
- **At most 64 stream spans waiting at once.** That counts reads in flight
  and spans whose read has returned and that wait to be ended and exported;
  there is one connection for each. A read counts until its I/O has
  returned. In a burst, only the first 64 get the read's data: 64 of 500 sync
  streams finishing at once, in each of 5 bursts (measured at `f1866eb`);
  and 145 of 160 streams arriving at 20 a second for 8 s while an inline
  exporter took 0.1 s per span (the other 15 were skipped, `at_capacity`,
  with 64 pending at the peak), measured at `4767825`.
- **When the span ends.** When the read returns, or when the budget runs
  out, whichever is first; either way its end time is the stream's end, not
  the read's. A read that is not made, fails or runs out of time leaves the
  span OK with its output, without the status, `predict_time` and token
  counts, and with `replicate.stream.read` saying why. A background thread
  ends stream spans, one at a time; with an exporter that runs inline their
  exports queue there, so spans arrive later, but no read waits behind them.
- **Flush, shutdown, exit and `uninstrument()` do not wait by default.**
  Each ends every stream span still waiting for its read at once, with what
  is known, so it is exported: `replicate.stream.read` = `flushed` for
  `force_flush()`, `shutdown` for `shutdown()` (including the SIGTERM/SIGINT
  handler `register()` installs) and interpreter exit, `uninstrumented` for
  `uninstrument()` (a `shutdown()` right after it still exports those spans).
  The one span the package's finisher thread may be exporting at that moment
  has already ended; that export is waited for (at most 5 s), so a shutdown or
  a process exit right after does not cut it off. A stream that completes
  after `uninstrument()` or `shutdown()` makes no read (`after_uninstrument`,
  `after_shutdown`). A flush after every request
  therefore costs nothing extra, but its stream spans carry no status or
  token counts unless the read had already returned. To wait, pass
  `stream_flush_wait_seconds` (for example in a serverless handler that
  freezes after it returns): `force_flush(timeout_millis)` then waits that
  long for reads in flight, never more than half of `timeout_millis`
  (default `30000`), and the provider's own flush gets only the time left, so
  with a batch span processor (`register()`'s default) the call never takes
  longer than you asked; with one that exports inline, each span it ends is
  exported inside the call, which can take longer. Its result is what
  `force_flush` returns. `stream_shutdown_wait_seconds` does the same for
  `shutdown()` and interpreter exit (no timeout of their own: they wait that
  long at most). The provider is shared: a flush ends (or, with the wait,
  waits for) every request's stream spans, not only its own. With an exporter
  that runs inline, ending a span exports it inside the call, so a shutdown,
  an exit or a flush still takes the export time of the stream spans waiting
  at that moment: **up to 64 times your exporter's per-span export time**
  (shared between the exiting thread and the finisher). Measured at
  `4767825`, with every stream's read still in flight when the script's last
  line returned: 30 streams and 1.5 s per export, exit 23.6 s later; 20
  streams and 0.2 s per export, 4.1 s; the same 20 with a batch span
  processor, 0.24 s, and with an exporter that takes no time, 0.04 s. That is
  export, not a wait for a read; an OTLP exporter that waits out its timeout
  against a collector that is down makes it minutes. A batch span processor
  (`register()`'s default) only queues the spans there.
- **Forks.** The package coordinates one thing with `os.fork()`: the fork
  waits (at most 5 s) until its finisher thread is between two span ends, so
  a child does not inherit a lock that thread held while exporting. A child
  starts its own worker threads when it needs them, and neither ends nor
  exports a stream span its parent was waiting on; the parent does. Whether
  a span processor or exporter works in a forked child (for example a
  `BatchSpanProcessor` whose worker thread was running) is up to that
  processor; the package makes no claim about it. With the read off there is
  no finisher and the fork waits for nothing.
- **What is kept.** Only the prediction's id, status, version, model,
  metrics and error string. Its input, output and logs are read with it and
  dropped.

`replicate.stream.skip_reason`, with `replicate.stream.read` = `skipped`:

| Reason | No read because |
|---|---|
| `custom_transport` | The client's transport for the API URL is not replicate's own `RetryTransport` over a plain `httpx` transport without a proxy: a transport of your own, a subclass, a transport or pool changed on the instance, or a client with `proxy=`. |
| `patched_http` | `httpx`'s or `httpcore`'s send path was replaced on the class (an HTTP mock such as respx, pytest-httpx or VCR.py, or `mock.patch`) when the stream completed or when the read was about to be sent. |
| `at_capacity` | 64 stream spans were already waiting, or 64 reads were live. |
| `no_provider_hook` | The provider's `force_flush` / `shutdown` could not be hooked (for example `instrument()` before `register()`). |
| `after_uninstrument` | The stream completed after `uninstrument()`. |
| `after_shutdown` | The stream completed after the provider's `shutdown()` (its span is not exported). |
| `client_closed` | The client's `httpx` client was closed. |
| `no_prediction_id` | The stream's prediction has no usable id. |
| `start_failed` | Planning or starting the read raised (for example no new thread during interpreter shutdown). |
| `interrupted` | A `KeyboardInterrupt` arrived while the read was planned; the span ends first, then it propagates. |

### create, wait and cancel

`predictions.create` returns a prediction that is usually still `starting`.
The span stays open on the returned prediction:

- `wait()` or `cancel()` on it (or `predictions.cancel(prediction.id)`) ends
  that same span with the final status. Create followed by wait is one span.
  While that `wait()` or `cancel()` runs, the span belongs to it: the cap, a
  `force_flush()` and releasing the prediction object leave it alone, so the
  final status, output and duration are recorded however long the prediction
  runs. Only `shutdown()` on the provider (including the SIGTERM/SIGINT
  handler `register()` installs) or interpreter exit ends it earlier, as a
  best effort: as of that moment, with the last status the client polled and
  no output, so the span is exported before the processors stop. The call
  still returns its result to you; it ends no second span.
- If neither is called, the span ends with the status the create response
  carried (`starting` or `processing`) and the time `create` returned. That
  happens at the first of: the next traced call after the prediction object
  is released or after the span has been held for `max_pending_seconds`;
  `force_flush()` or `shutdown()` on the tracer provider (see
  [which provider](#which-provider)); `uninstrument()`; interpreter exit. A
  `wait()` or `cancel()` after that gets its own span. Such a span is not a
  completion. This package never polls a prediction you did not wait for.

**Deviation from spec J2.4.** The spec says a create that is not waited on
ends at create. Ending it there would make create followed by `wait()` two
spans, and AC-03 asks for one; AC-02 asks that a create nobody waits on
records the create-time status. Both hold here because the span is held
open on the returned prediction instead, and when nothing continues it, it
is ended with the create-time status and end timestamp (AC-02). What is
deferred is delivery, not the recorded data. Delivery is bounded:

- **Cap.** A span is held for at most `max_pending_seconds` (default 600).
  An older held span is ended, as of create time and with its create-time
  status, the next time any traced call runs or the provider is flushed or
  shut down. A later `wait()` on that prediction gets its own
  `replicate.prediction.wait` span.
- **Flush and shutdown.** `force_flush()` and `shutdown()` on the provider
  end every held span first, so a flush at the end of a request, a container
  stop (SIGTERM) or Ctrl-C (SIGINT) under `register()` exports it. A flush
  leaves a span whose `wait()`/`cancel()` is still running to that call;
  `shutdown()` ends it too (see above). `uninstrument()` restores both
  methods.

#### Which provider

The hooks go on the tracer provider you pass to `instrument()`. Without
`tracer_provider=`, they go on the global provider at the time
`instrument()` runs. Call `fi_instrumentation.register()` (or set your own
global provider) **before** `instrument()`: until a provider is set, the
global one is OpenTelemetry's placeholder, which has no `force_flush` or
`shutdown`, so no hook is installed and held spans then depend on the exit
hook alone, which runs after `register()`'s shutdown has stopped the
processors. Without a hook the opt-in read after a stream is not made either
(nothing could end its span at a flush or at shutdown): stream spans end at
once, without status or token counts, with `replicate.stream.read` =
`skipped` and `replicate.stream.skip_reason` = `no_provider_hook`.

The prediction you get back is the client's `Prediction` behind a thin
`wrapt.ObjectProxy`: `isinstance(p, Prediction)` holds, and fields, methods,
`dict()`, copying and pickling behave as the client's (a copy or an unpickled
object is a plain `Prediction`). Only `type(p) is Prediction` is false for
an object a traced `create` returned while it was not yet finished.

## Span attributes

| Attribute | Value |
|---|---|
| `gen_ai.provider.name` | `replicate` |
| `gen_ai.request.model` | `owner/name` from the model ref; for a version-only or deployment call, the `model` the prediction reports. |
| `replicate.prediction.version` | The version id, when the ref or the prediction has one. The placeholder `hidden` that official models (for example `meta/meta-llama-3-8b-instruct`) report is not a version and is not recorded. |
| `replicate.deployment` | `owner/name` of the deployment, for deployment calls. |
| `replicate.prediction.id` | The prediction id. `run` and `stream` record the id of the prediction the client created for them. Absent when the client never got one (for example an invalid ref). |
| `replicate.prediction.status` | The status string the client returns (`starting`, `processing`, `succeeded`, `failed`, `canceled`), never normalised; the opt-in read after a stream also knows `aborted` (the prediction's deadline passed before it started). **Stream spans have none by default**: the event stream does not report one. With the opt-in read they have the status it found, or none when it was skipped, failed or ran out of time (see [the opt-in read after a stream](#the-opt-in-read-after-a-stream)). Two exceptions: a `done` event whose data says `{"reason": "canceled"}` records `canceled`, read or not, and with the read on, a stream that ended without `done` records the non-final status the API answered (for example `processing`). A `replicate.run` span can show `processing` on an OK span with output: with `Prefer: wait` (the default `wait=True` in replicate 1.0.7, an explicit `wait=` in 1.0.0) the client's `run()` returns without polling once the create response is no longer `starting`, and the server can answer `processing` for a prediction that has finished (the client's `run.py` behaviour). |
| `replicate.metrics.predict_time` | `metrics.predict_time` in seconds, when present. A duration, not a price. |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens` | `metrics.input_token_count` and `metrics.output_token_count`, and their sum, when the prediction reports them (Replicate's language models do). Never estimated. Stream spans have them only from the opt-in read after `done`. |
| `replicate.output.type` | `text`, `url` (one file URL), `list`, `object` or `other`. |
| `replicate.stream.file_count` | The number of file outputs in a stream whose every output event was a URL or a file. A text stream has none, even when some of its chunks start like a URL. |
| `replicate.stream.read` | Only with the opt-in read: why a stream span has no final status from it. `skipped` (not made; `replicate.stream.skip_reason` says why), `timeout` (the budget ran out), `failed` (refused, for example HTTP 429; unreadable; over 2 MiB; or no name lookup), `unfinished` (the API still answered a non-final status twice), `flushed` / `shutdown` / `uninstrumented` (a `force_flush()`, a `shutdown()` or interpreter exit, or `uninstrument()` ended the span before the read returned). Absent when the read found the final status, and always absent with the read off (the default). |
| `replicate.stream.skip_reason` | With `replicate.stream.read` = `skipped`: `custom_transport`, `patched_http`, `at_capacity`, `no_provider_hook`, `after_uninstrument`, `after_shutdown`, `client_closed`, `no_prediction_id`, `start_failed` or `interrupted` (see [the opt-in read after a stream](#the-opt-in-read-after-a-stream)). |
| `replicate.stream.done_reason` | The `reason` in the data of the stream's `done` event, when there is one (`canceled`). |
| `replicate.stream.truncated` | `true` when the event stream ended without a `done` event. The span is OK with the output received, and its status is what the API answered for the prediction. |
| `gen_ai.span.kind` | `LLM` when the output is text, `CHAIN` otherwise (files, no output yet, errors). |
| `input.value`, `input.mime_type` | The prediction `input` as JSON. |
| `output.value`, `output.mime_type` | Text output: a list of text tokens is joined, and stream and iterator text events are concatenated, in order, including chunks that start like a URL (`https://`, `http://`, `data:`) and, in replicate 1.0.7, the `FileOutput` it makes of such a chunk (as its URL). Only when every element or event is a URL or a file is the output the file URL(s), as strings (a stream records only their count). |
| `gen_ai.request.parameters` | JSON of the call options that were passed: `wait`, `stream`, `use_file_output`, `file_encoding_strategy`, `webhook_events_filter`; `webhook` / `webhook_completed` only as `true`. |
| `replicate.cancelled` | `true` when the caller abandoned the call (closed/dropped an iterator, cancelled a task); an explicit `cancel()` is recorded as status `canceled`. See [errors and cancellation](#errors-and-cancellation). |

Token counts are recorded only as the prediction reports them; no cost is
recorded.

## Privacy

- **Content follows `TraceConfig`.** Inputs and outputs are recorded by
  default, like every traceAI LLM integration. Hide them with
  `TraceConfig(hide_inputs=True, hide_outputs=True)` or `FI_HIDE_INPUTS=true`
  / `FI_HIDE_OUTPUTS=true`: `input.value` / `output.value` become
  `__REDACTED__`, and with outputs hidden a failed prediction's error string
  (span status and exception message) is `__REDACTED__` too.
  With `pii_redaction` / `FI_PII_REDACTION=true`, PII is replaced by tokens
  such as `<EMAIL_ADDRESS>` in every attribute and, after the token is
  removed, in the error status and the exception event.
  Replicate still receives the input either way.
- **The API token is never recorded.** Wherever the client stored it (the
  `api_token` argument, an `Authorization` header passed to `Client`, or the
  header the client built from `REPLICATE_API_TOKEN`), it is replaced by
  `[redacted]` in every recorded value, status and exception. The package
  itself never reads `REPLICATE_API_TOKEN`.
- **No stack trace, local path or user name is exported.** The exception
  event carries `exception.type` and `exception.message` only, in every
  mode; the Python traceback (your file paths, user name and virtualenv) is
  never recorded.
- **Files are never fetched.** File outputs are recorded as URL strings; the
  package never reads a `FileOutput`. A `data:` URI keeps only its media type
  (`data:image/png;base64,<N characters omitted>`), because its payload is the
  file. In inputs, file handles, paths and bytes are described, not read.
- **Webhooks stay yours.** A webhook URL is recorded only as present. This
  package hosts no webhook receiver and never sets `webhook=`.

## Errors and cancellation

- An exception from the client (`ModelError` for a failed `run`,
  `ReplicateError` for an HTTP error, `ValueError` for a bad ref) sets the
  span to ERROR, records one exception event (type and message, no stack
  trace), and is re-raised unchanged. The span kind follows the output, so a
  failed call (no output) is `CHAIN` even for a language model: count failed
  calls by status, not by kind.
- The client does not retry a create (POST) answered 429; the span ends
  ERROR and a retry by the caller is a second span. Reads (GET) answered 429,
  503 or 504 are retried inside the client, within the same span, except the
  package's own opt-in read after a stream, which is never retried.
- A prediction that ends `failed` (for example after `wait()`, which does not
  raise) sets the span to ERROR with the prediction's `error` string.
- A prediction that ends `canceled` is a terminal state, not a failure: the
  span status is OK, with `replicate.prediction.status` = `canceled` and no
  exception event.
- When the caller abandons the call — an `asyncio` task is cancelled, or a
  stream/iterator is closed (`close()` / `aclose()`) or dropped before its
  end (for a stream, before its `done` event) — the span ends once with
  status ERROR, description `cancelled`, and `replicate.cancelled` = `true`,
  without an exception event. A stream closed or dropped before its first
  `next()` sent no request and has no span.
- Nothing is exported from a finaliser. The garbage collector can release a
  dropped stream, iterator or held prediction on any thread, even while that
  thread holds an exporter's lock, so its span end is only queued then. It is
  done, with the time the object was dropped (create time for a held
  prediction), at the next traced call, the provider's `force_flush()` or
  `shutdown()`, `uninstrument()`, or interpreter exit. After
  `uninstrument()` the provider's methods are no longer hooked, so a stream
  still open then and dropped later is ended only at interpreter exit; if
  the provider was shut down before that, its span is not exported. Close
  such a stream (or run it to `done`) before `uninstrument()`.
- An error inside this instrumentation is logged at DEBUG and never changes
  the client's result or exception.

## Limits

- `Prediction.stream()` and `Prediction.output_iterator()` on a prediction
  you created yourself are not wrapped; use `Client.stream` / `run` for a
  traced stream.
- The `Cancel-After` deadline is not recorded: replicate 1.x has no per-call
  option for it.
- A token that exists only in the environment is known to the redaction once
  the client has made its first request. If a call fails before any request
  (for example on an invalid ref) and its input contains that token, the
  token is not redacted from that span.
- Do not combine this package with another instrumentor that wraps the same
  Replicate client methods, or spans may be duplicated.
- Stream spans have no status, `predict_time` or token counts unless you opt
  in to the read after a stream, and even then not with a transport of your
  own other than a plain `httpx` one, nor while an HTTP mock patches `httpx`
  (see [the opt-in read after a stream](#the-opt-in-read-after-a-stream)).

## Live vendor verification (2026-10-20 to 2026-10-21 UTC)

Seven sessions against the live `api.replicate.com`, with `replicate` 1.0.7
on Python 3.12 and `meta/meta-llama-3-8b-instruct` (`max_tokens` 24), exported
to a local Future AGI stack. Fourteen predictions were made in total: seven
in the first session, two in the second, and one each in the third, the
fourth (00:32 UTC on 2026-10-21), the fifth (02:37 UTC), the sixth (05:15
UTC) and the seventh (06:27 UTC); the account's prediction list since
2026-10-20 shows exactly those fourteen. The
second session ran the read after `done` as it was then, on the caller's
path; the third (code `ab5f5c4`) on a background thread started per stream,
through the client's own transport; the fourth (code `22832fd`) through the
package's own connection pool; the fifth (code `b5df956`) through that pool
for a plain `httpx` transport. Sessions 2 to 5 ran with the read after `done`
on, which was the default until round 4 of the review, on code from before
round 4; **the current opt-in read (round 4 on) has not been run live**. The
sixth (code `f1866eb`, sync) and the seventh (code `4767825`, async) ran the
current default, with no read.

Stored in Future AGI and read back, one trace each:

- `replicate.async_stream` with the default settings (seventh session, code
  `4767825`): the module function inside `asyncio.run()`, after
  `instrument(tracer_provider=...)` with no other option. The events were
  `output` x6 and `done`. The span ended on the caller's thread inside the
  `__anext__()` that returned `done` (it took 1.4 ms, reading the event
  included; the span's end time is 0.15 ms before it returned), the loop
  exited 0.45 ms after `done`, and `asyncio.run()` returned 0.58 ms after
  the main coroutine had finished. Counted at httpx's transport classes, the
  stream sent the create and the event stream, and nothing was sent after
  it, during `shutdown()` or in the 0.5 s after it. The thread names were the
  same before and after `instrument()`, after `asyncio.run()` and after the
  flush, none of them the package's. The stored span is LLM, OK, with the
  prediction id and the joined text, and no status, `predict_time`, token
  counts (stored as 0) or `replicate.stream.*` attribute. It is the only span
  in its trace.
- `replicate.stream` with the default settings (sixth session, code
  `f1866eb`): the module function `replicate.stream`, sync, after
  `instrument(tracer_provider=...)` with no other option. The events were
  `output` x5 and `done`. The span ended on the caller's thread inside the
  `next()` that returned `done` (that `next()` took 2.4 ms, reading the event
  included; the span's end time is 0.27 ms before it returned), and the loop
  exited 1.7 ms after `done`. Counted at httpx's transport classes, the
  stream sent the create and the event stream, and nothing was sent after it
  up to the flush (that run did not count requests during `shutdown()`; the
  seventh did); the thread names were the same before and after
  `instrument()` and after the stream, none of them the package's.
  `shutdown()` took 0.15 ms. The
  stored span is LLM, OK, with the prediction id and the joined text, and no
  status, `predict_time`, token counts (stored as 0) or `replicate.stream.*`
  attribute. It is the only span in its trace.
- `replicate.run`, sync (first session): LLM, OK, with prediction id, status
  `succeeded`, `predict_time` and the token counts Replicate reports
  (`input_token_count` / `output_token_count`, 33 / 9). The output arrives as
  a list of text chunks and is recorded as joined text. This row was written
  before the placeholder version `hidden` was dropped and still carries it.
- `async_run` (second session): the same `replicate.run` span (LLM, OK,
  prediction id, `succeeded`, `predict_time`, 34 / 17 tokens), with no
  version attribute.
- `replicate.stream` (second session): the events were `output` (5 of them;
  3 in the first session) and then `done`, with no status or usage in the
  stream. The span is LLM, OK, with the prediction id and the joined text,
  and, from the read after `done`, status `succeeded`, `predict_time` and
  34 / 19 tokens; no version attribute. The first session's stream row, from
  before that read existed, has only the prediction id, the text and the
  `hidden` version.
- `replicate.stream`, breaking out at `done` (third session, code `ab5f5c4`):
  the caller's loop stopped at the `done` event (after 4 `output` events) and
  closed the stream. The `next()` that returned `done` took 0.19 ms and
  `close()` 1.3 ms; the read ran on the background thread, and the span
  ended there 73 ms after the caller had left the loop, with its end time at
  the stream's end. The stored span is LLM, OK, not `cancelled`, with the
  joined text and, from the read, status `succeeded`, `predict_time` and
  39 / 15 tokens; no version attribute. It is the only span in its trace:
  the read added none.
- `replicate.async_stream` on a client whose transport replicate built itself
  (fourth session, code `22832fd`): the read after `done` went through the
  package's own connection pool on its background event loop. The
  `__anext__()` that returned `done` took 0.42 ms; the span ended on the
  package's finisher thread 104 ms after the loop had exited, with the
  stream's end as its end time, and no sync HTTP client was built on the
  `Client`. The read (which always asks for `Accept-Encoding: identity`)
  returned the final status. The stored span is LLM, OK, with the joined
  text and, from the read, status `succeeded`, `predict_time` and 39 / 17
  tokens, no `replicate.stream.read` and no version attribute. It is the only
  span in its trace.
- `replicate.async_stream` inside `asyncio.run()`, on
  `replicate.Client(transport=httpx.AsyncHTTPTransport(retries=2))` (fifth
  session, code `b5df956`): the read went through the package's own pool,
  built with that transport's TLS context; your transport saw the create,
  the event stream and a model lookup, and no read of the prediction.
  Afterwards the package's name-lookup cache held one entry (the run counted
  cache entries, not lookups). The
  `__anext__()` that returned `done` took 0.20 ms, `asyncio.run()` returned
  0.73 ms after the main coroutine had finished, and the span ended on the
  package's finisher thread 125 ms after the loop had exited, with the
  stream's end as its end time. The stored span is LLM, OK, with the joined
  text and, from the read, status `succeeded`, `predict_time` and 40 / 14
  tokens, no `replicate.stream.read` and no version attribute. It is the only
  span in its trace.
- An unknown version (second session): HTTP 422 "Invalid version or not
  permitted". The span is ERROR, kind `CHAIN` (no output), with the 422
  detail as its status message and one `exception` event (type and message,
  no stack trace). No prediction is created; the requested version id is
  recorded.
- A rate limit (first session): HTTP 429 "Request was throttled" on a
  create. The span is ERROR with Replicate's message, which states the
  account's rate limit and credit threshold; vendor error text is stored as
  the vendor wrote it.

Not live-verified, stand-in only: `predictions.create` followed by `wait()`
(the held create span), `cancel`, deployments, run iterators, image and file
outputs, text chunks that start like a URL, and the opt-in read as it is
now (round 4 on), including its `custom_transport` and `patched_http`
skips. Trainings are not traced (checked with the stand-in).

The stored rows of that project (spans, traces, rollups, the attribute
catalog and the project row: 137 rows after the seventh session) were scanned:
the API token appears 0 times (whole or its last 12 characters), and there is
no `r8_`, `Bearer`, `Authorization`, `/Users/` path, login or host name,
`Traceback`, `site-packages` or `exception.stacktrace`.
