# traceAI-replicate

OpenTelemetry instrumentation for the official
[`replicate`](https://github.com/replicate/replicate-python) Python client.

Replicate's API is predictions, not OpenAI chat completions, so the OpenAI
instrumentor does not trace this client: pointing `openai.OpenAI` at
`api.replicate.com` traces nothing. This package wraps the Replicate client
itself. It does not set or rewrite the client's base URL.

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

`instrument()` accepts `tracer_provider`, `config` (a
`fi_instrumentation.TraceConfig`), `max_pending_seconds` (a positive
number, default `600`; see [create, wait and cancel](#create-wait-and-cancel))
and `stream_reload_seconds` (from `0` to `10` seconds, default `2`; see
[the read after a stream](#the-read-after-a-stream)). Any other option, a
`config` that is not a `TraceConfig`, or a `max_pending_seconds` /
`stream_reload_seconds` that is not an `int` or `float` raises `TypeError`; a
`max_pending_seconds` that is not a positive, finite number, or a
`stream_reload_seconds` outside 0 to 10, raises `ValueError`.

A call made while OpenTelemetry instrumentation is suppressed in the
caller's context (`_SUPPRESS_INSTRUMENTATION_KEY`) is not traced at all.

## Instrumented calls

| Call | Span |
|---|---|
| `Client.run`, `async_run` (and `replicate.run`) | One `replicate.run` span. The create request and the client's own polling run inside it and add no span. If the client returns an iterator, the span stays open until the iterator is exhausted, closed, or dropped. |
| `Client.stream`, `async_stream` (and `replicate.stream`) | One `replicate.stream` span for the whole stream, not one per event. The client's stream is lazy: it sends nothing until it is first iterated, so the span starts at the first `next()` (as a child of the span that was current when you called `stream()`), and a stream that is never iterated has no span. The stream is complete at its `done` event; breaking out of the loop there and closing or dropping the stream is a completed stream (OK, with its output), not a cancellation. The event stream reports no status or metrics, so the prediction is then read once more in the background; see [the read after a stream](#the-read-after-a-stream). Your loop gets each event, `done` included, without waiting for that read. A stream that raises, or is closed or dropped before `done`, makes no read. |
| `predictions.create`, `async_create`, also `models.predictions.create` and `deployments.predictions.create` | One `replicate.predictions.create` span. See [create, wait and cancel](#create-wait-and-cancel). |
| `Prediction.wait`, `async_wait` | Ends the open create span of that prediction, or opens one `replicate.prediction.wait` span. A `wait()` on a prediction that is already finished makes no request and adds no span. |
| `Prediction.cancel`, `async_cancel`, `predictions.cancel(id)`, `async_cancel(id)` | Ends the open create span of that prediction, or opens one `replicate.predictions.cancel` span. |
| `trainings.*`, `predictions.get` / `list`, `reload`, file uploads | Not traced. A training call produces no span from this package. |

Each span is a child of the active span when there is one. It is current
while the client sends its requests, so spans from an HTTP client
instrumentation nest under it. The read after a stream is the exception: it
runs with OpenTelemetry instrumentation suppressed and adds no span.

### The read after a stream

The event stream carries no status, `predict_time` or token counts; the
prediction does. After `done`, the package reads the prediction once
(`GET /v1/predictions/{id}`) and records those on the stream span.

- **Cost and rate limit.** The read creates no prediction and is not billed,
  but it is an API request: it counts toward your account's rate limit for
  requests other than creates (3000 per minute by
  [Replicate's rate-limit page](https://replicate.com/docs/topics/predictions/rate-limits)),
  one per stream, two when the API still answers `processing` for a
  prediction that has just sent `done`. To turn it off, pass
  `stream_reload_seconds=0` to `instrument()`; stream spans then carry no
  status (unless `done` said `canceled`), `predict_time` or token counts.
- **Only for a span that is exported.** No read is made for a span that is
  not recording (a trace your sampler dropped, `NoOpTracerProvider`), after
  the provider is shut down, or when no flush/shutdown hook could be
  installed on the provider (see [which provider](#which-provider)): there
  the stream span ends at once, without it.
- **Off your path.** Completing a stream hands it to a background worker
  through a queue. Your `next()` / `__anext__()` does not wait for the read,
  does no I/O for it and starts no thread (the first completed stream in a
  process starts the worker thread once), and a timeout you put around the
  stream (for example `asyncio.timeout`) is not affected. The read's own work
  still runs in your process and competes for the GIL: with 200 streams
  finishing at once against a local stand-in, the time from `done` to the
  end of each loop was 10 to 35 ms longer at the median than without the
  read.
- **Where it goes.** When `replicate.Client` built its own transport (you
  passed no `transport=` or `mounts=`), the read goes through a connection
  pool this package owns (at most 16 connections), with your client's base
  URL, headers and proxy settings, on a background event loop. It never uses
  your client's connection pool and never creates an HTTP client on your
  `Client`: an app that only calls `async_stream` keeps only its async
  client. When you passed a transport, the read has to go through it: on a
  background thread when it can send synchronously (also for
  `async_stream`), or, for a transport that is only async, as a task on your
  running event loop. At most 4 such reads run at once, and when your
  transport is an `httpx` transport the package can see, at most one per 8
  of its connections (none below 8), so a read never takes a connection your
  own requests need.
- **Bounded.** `stream_reload_seconds` (default `2`, at most `10`) is a
  wall-time budget from the stream's end. The read is one request, and one
  more `poll_interval` later only when the prediction is not finished yet.
  The package never retries it: a `429` or `5xx` ends it, and `Retry-After`
  is not waited on. Replicate's `RetryTransport` layers on top are bypassed;
  a transport of yours that retries by itself (or wraps a `RetryTransport`)
  is used as it is. Through the package's own pool the budget is an absolute
  deadline: when it runs out the request is cancelled and its connection
  closed, in whatever phase it is (name lookup, connect, headers, body), and
  name lookups run on at most 2 background threads. Through your transport,
  each phase gets at most the time left, but a server that sends bytes slowly
  can keep that read, one of its 4 slots and one of your transport's
  connections until your transport's own timeouts end it; the span still
  ends at the deadline. The body is limited to 2 MiB as it arrives; the read
  asks for an uncompressed body, and a compressed one is inflated in bounded
  steps.
- **At most 64 reads at once** through the package's pool (4 through your
  transport). A read counts until its I/O has returned, also past its
  deadline. A stream that completes while that many are running ends at
  once without one, with `replicate.stream.read` = `skipped`.
- **When the span ends.** When the read returns, or when the budget runs
  out, whichever is first; either way its end time is the stream's end, not
  the read's. A read that is not made, fails or runs out of time leaves the
  span OK with its output, without the status, `predict_time` and token
  counts, and with `replicate.stream.read` saying why.
- **Flush.** `force_flush(timeout_millis)` waits for reads in flight for at
  most half of `timeout_millis` (default `30000`), never past their budget.
  A read still running then is cut short, and its span ends with what is
  known (`replicate.stream.read` = `timeout`), so that flush exports it. The
  provider's own flush gets the rest of the timeout, so the call never takes
  longer than you asked, and its result is what `force_flush` returns. The
  provider is shared: a flush waits for every request's reads in flight, not
  only its own. With a transport of yours that is only async, a
  `force_flush()` called on the event loop that runs the read cannot wait
  for it and ends its span at once.
- **Shutdown, exit and `uninstrument()`.** `shutdown()` and interpreter exit
  wait for reads in flight at most until their budget runs out (default 2 s),
  then end their spans, so they are exported: a script that streams exits
  about one read later (0.26 s for a 0.2 s read, 2.05 s when the API does
  not answer, measured). `uninstrument()` does not wait: the worker ends each
  span when its read returns or its budget runs out, on the provider, which
  is still running. A Ctrl-C during a wait ends the pending spans first.
  When `asyncio.run()` ends, a read already running on that loop (an
  async-only transport of yours) gets the rest of its budget.
- **Forks.** A child forked while a read is in flight neither ends nor
  exports that span; the parent does.
- **What is kept.** Only the prediction's id, status, version, model,
  metrics and error string. Its input, output and logs are read with it and
  dropped.

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
processors. Without a hook the read after a stream is not made either
(nothing could wait for it at a flush or at exit): stream spans end at once,
without status or token counts, with `replicate.stream.read` = `skipped`.

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
| `replicate.prediction.status` | The status string the client returns (`starting`, `processing`, `succeeded`, `failed`, `canceled`), never normalised; the read after a stream also knows `aborted` (the prediction's deadline passed before it started). Stream spans have the status of the read after `done` (see [the read after a stream](#the-read-after-a-stream)), or none when that read is off, failed or ran out of time: the event stream does not report one. Two exceptions: a `done` event whose data says `{"reason": "canceled"}` records `canceled` even without the read, and a stream that ended without `done` records the non-final status the API answered (for example `processing`). A `replicate.run` span can show `processing` on an OK span with output: with `Prefer: wait` (the default `wait=True` in replicate 1.0.7, an explicit `wait=` in 1.0.0) the client's `run()` returns without polling once the create response is no longer `starting`, and the server can answer `processing` for a prediction that has finished (the client's `run.py` behaviour). |
| `replicate.metrics.predict_time` | `metrics.predict_time` in seconds, when present. A duration, not a price. |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens` | `metrics.input_token_count` and `metrics.output_token_count`, and their sum, when the prediction reports them (Replicate's language models do). Never estimated. |
| `replicate.output.type` | `text`, `url` (one file URL), `list`, `object` or `other`. |
| `replicate.stream.file_count` | File outputs seen in a stream, when there were any. |
| `replicate.stream.read` | Why a stream span has no final status from the read after `done`: `skipped` (not made: 64 reads were already running, the client was closed, or the provider has no hook), `timeout` (the budget, a flush or the end of your event loop came first), `failed` (refused, for example HTTP 429; unreadable; or over 2 MiB), `unfinished` (the API still answered a non-final status twice). Absent when the read found the final status, or is off. |
| `replicate.stream.done_reason` | The `reason` in the data of the stream's `done` event, when there is one (`canceled`). |
| `replicate.stream.truncated` | `true` when the event stream ended without a `done` event. The span is OK with the output received, and its status is what the API answered for the prediction. |
| `gen_ai.span.kind` | `LLM` when the output is text, `CHAIN` otherwise (files, no output yet, errors). |
| `input.value`, `input.mime_type` | The prediction `input` as JSON. |
| `output.value`, `output.mime_type` | Text output (a list of text tokens is joined; stream text events are concatenated), or the file URL(s) as strings. |
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
  package's own read after a stream, which the package never retries (a
  transport of yours that retries by itself is used as it is).
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
  `shutdown()`, `uninstrument()`, or interpreter exit.
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
- The read after a stream through a transport you passed (see
  [where it goes](#the-read-after-a-stream)) cannot be cancelled from outside
  when it sends synchronously: a server that trickles bytes keeps it, and one
  of your transport's connections, until your transport's own timeouts end
  it. At most 4 are live at once, and the span always ends at the deadline.
  If such a transport wraps an `httpx` pool of fewer than 8 connections that
  the package cannot see, keep `stream_reload_seconds` low or `0`.

## Live vendor verification (2026-10-20)

Four sessions against the live `api.replicate.com`, with `replicate` 1.0.7
on Python 3.12 and `meta/meta-llama-3-8b-instruct` (`max_tokens` 24), exported
to a local Future AGI stack. Eleven predictions were made in total: seven in
the first session, two in the second, one in the third and one in the fourth
(00:32 UTC on 2026-10-21); the account's prediction list since 2026-10-20
shows exactly those eleven. The second session ran the read after `done` as
it was then, on the caller's path; the third ran it on a background thread
started per stream, through the client's own transport; the fourth ran this
release, through the package's own connection pool.

Stored in Future AGI and read back, one trace each:

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
- `replicate.stream`, breaking out at `done` (third session, this release):
  the caller's loop stopped at the `done` event (after 4 `output` events) and
  closed the stream. The `next()` that returned `done` took 0.19 ms and
  `close()` 1.3 ms; the read ran on the background thread, and the span
  ended there 73 ms after the caller had left the loop, with its end time at
  the stream's end. The stored span is LLM, OK, not `cancelled`, with the
  joined text and, from the read, status `succeeded`, `predict_time` and
  39 / 15 tokens; no version attribute. It is the only span in its trace:
  the read added none.
- `replicate.async_stream` on a client whose transport replicate built itself
  (fourth session, this release): the read after `done` went through the
  package's own connection pool on its background event loop. The
  `__anext__()` that returned `done` took 0.42 ms; the span ended on the
  package's finisher thread 104 ms after the loop had exited, with the
  stream's end as its end time, and no sync HTTP client was built on the
  `Client`. Replicate answered the read's `Accept-Encoding: identity`
  request. The stored span is LLM, OK, with the joined text and, from the
  read, status `succeeded`, `predict_time` and 39 / 17 tokens, no
  `replicate.stream.read` (the read found the final status) and no version
  attribute. It is the only span in its trace.
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
outputs, and, in this release, the read after a stream through a transport
you passed (the third session ran the previous way of doing that). Trainings
are not traced (checked with the stand-in).

The stored rows of that project (spans, traces, rollups, the attribute
catalog and the project row: 137 rows after the fourth session) were scanned:
the API token appears 0 times (whole or its last 12 characters), and there is
no `r8_`, `Bearer`, `Authorization`, `/Users/` path, login or host name,
`Traceback`, `site-packages` or `exception.stacktrace`.
