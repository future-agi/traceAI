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
`fi_instrumentation.TraceConfig`) and `max_pending_seconds` (a positive
number, default `600`; see [create, wait and cancel](#create-wait-and-cancel)).
Any other option, a `config` that is not a `TraceConfig`, or a
`max_pending_seconds` that is not an `int` or `float` raises `TypeError`; a
`max_pending_seconds` that is not a positive, finite number raises
`ValueError`.

## Instrumented calls

| Call | Span |
|---|---|
| `Client.run`, `async_run` (and `replicate.run`) | One `replicate.run` span. The create request and the client's own polling run inside it and add no span. If the client returns an iterator, the span stays open until the iterator is exhausted, closed, or dropped. |
| `Client.stream`, `async_stream` (and `replicate.stream`) | One `replicate.stream` span for the whole stream, not one per event. |
| `predictions.create`, `async_create`, also `models.predictions.create` and `deployments.predictions.create` | One `replicate.predictions.create` span. See [create, wait and cancel](#create-wait-and-cancel). |
| `Prediction.wait`, `async_wait` | Ends the open create span of that prediction, or opens one `replicate.prediction.wait` span. A `wait()` on a prediction that is already finished makes no request and adds no span. |
| `Prediction.cancel`, `async_cancel`, `predictions.cancel(id)`, `async_cancel(id)` | Ends the open create span of that prediction, or opens one `replicate.predictions.cancel` span. |
| `trainings.*`, `predictions.get` / `list`, `reload`, file uploads | Not traced. A training call produces no span from this package. |

Each span is a child of the active span when there is one. It is current
while the client sends its requests, so spans from an HTTP client
instrumentation nest under it.

### create, wait and cancel

`predictions.create` returns a prediction that is usually still `starting`.
The span stays open on the returned prediction:

- `wait()` or `cancel()` on it (or `predictions.cancel(prediction.id)`) ends
  that same span with the final status. Create followed by wait is one span.
- If neither is called, the span ends with the status the create response
  carried (`starting` or `processing`) and the time `create` returned. That
  happens at the first of: the next traced call after the prediction object
  is released or after the span has been held for `max_pending_seconds`;
  `force_flush()` or `shutdown()` on the tracer provider you passed to
  `instrument()` (including the SIGTERM/SIGINT handler that `register()`
  installs, which calls `shutdown()`); `uninstrument()`; interpreter exit. A
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
  you passed to `instrument()` end every held span first, so a flush at the
  end of a request, a container stop (SIGTERM) or Ctrl-C (SIGINT) under
  `register()` exports it. `uninstrument()` restores both methods.

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
| `replicate.prediction.version` | The version id, when the ref or the prediction has one. |
| `replicate.deployment` | `owner/name` of the deployment, for deployment calls. |
| `replicate.prediction.id` | The prediction id. `run` and `stream` record the id of the prediction the client created for them. Absent when the client never got one (for example an invalid ref). |
| `replicate.prediction.status` | The status string the client returns (`starting`, `processing`, `succeeded`, `failed`, `canceled`), never normalised. Stream spans have none: the event stream does not report one. |
| `replicate.metrics.predict_time` | `metrics.predict_time` in seconds, when present. A duration, not a price. |
| `replicate.output.type` | `text`, `url` (one file URL), `list`, `object` or `other`. |
| `replicate.stream.file_count` | File outputs seen in a stream, when there were any. |
| `gen_ai.span.kind` | `LLM` when the output is text, `CHAIN` otherwise (files, no output yet, errors). |
| `input.value`, `input.mime_type` | The prediction `input` as JSON. |
| `output.value`, `output.mime_type` | Text output (a list of text tokens is joined; stream text events are concatenated), or the file URL(s) as strings. |
| `gen_ai.request.parameters` | JSON of the call options that were passed: `wait`, `stream`, `use_file_output`, `file_encoding_strategy`, `webhook_events_filter`; `webhook` / `webhook_completed` only as `true`. |
| `replicate.cancelled` | `true` when the caller cancelled (see below). |

No token usage and no cost are recorded, whatever the prediction's metrics
contain.

## Privacy

- **Content follows `TraceConfig`.** Inputs and outputs are recorded by
  default, like every traceAI LLM integration. Hide them with
  `TraceConfig(hide_inputs=True, hide_outputs=True)` or `FI_HIDE_INPUTS=true`
  / `FI_HIDE_OUTPUTS=true`: `input.value` / `output.value` become
  `__REDACTED__`, and with outputs hidden a failed prediction's error string
  (span status, exception message and stack trace) is `__REDACTED__` too.
  Replicate still receives the input either way.
- **The API token is never recorded.** Wherever the client stored it (the
  `api_token` argument, an `Authorization` header passed to `Client`, or the
  header the client built from `REPLICATE_API_TOKEN`), it is replaced by
  `[redacted]` in every recorded value, status and exception. The package
  itself never reads `REPLICATE_API_TOKEN`.
- **Files are never fetched.** File outputs are recorded as URL strings; the
  package never reads a `FileOutput`. A `data:` URI keeps only its media type
  (`data:image/png;base64,<N characters omitted>`), because its payload is the
  file. In inputs, file handles, paths and bytes are described, not read.
- **Webhooks stay yours.** A webhook URL is recorded only as present. This
  package hosts no webhook receiver and never sets `webhook=`.

## Errors and cancellation

- An exception from the client (`ModelError` for a failed `run`,
  `ReplicateError` for an HTTP error, `ValueError` for a bad ref) sets the
  span to ERROR, records one exception event, and is re-raised unchanged.
- A prediction that ends `failed` (for example after `wait()`, which does not
  raise) sets the span to ERROR with the prediction's `error` string.
- A prediction that ends `canceled` is a terminal state, not a failure: the
  span status is OK, with `replicate.prediction.status` = `canceled` and no
  exception event.
- When the caller abandons the call — an `asyncio` task is cancelled, or a
  stream/iterator is closed (`close()` / `aclose()`) or dropped before its
  end — the span ends once with status ERROR, description `cancelled`, and
  `replicate.cancelled` = `true`, without an exception event.
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
