# traceAI-parallel

OpenTelemetry instrumentation for the Search and Extract methods of the
[`parallel-web`](https://pypi.org/project/parallel-web/) client
(Parallel Web Systems).

It traces `search` and `extract` only. The Task API (`task_run`,
`task_group`), Monitor, FindAll and the rest of `client.beta` are different
products and produce no spans from this package.

## Installation

```bash
pip install traceAI-parallel
```

It accepts `parallel-web>=1.0.1,<2` and `fi-instrumentation-otel>=1.1.0`.
The suite runs with `parallel-web` 1.0.1 and 1.3.5 on Python 3.10, 3.11, 3.12
and 3.13.

## Usage

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from parallel import Parallel
from traceai_parallel import ParallelInstrumentor

tracer_provider = register(project_type=ProjectType.OBSERVE, project_name="parallel-search")
ParallelInstrumentor().instrument(tracer_provider=tracer_provider)

client = Parallel()  # reads PARALLEL_API_KEY
result = client.search(search_queries=["open telemetry retrieval"], mode="turbo")
client.extract(urls=[result.results[0].url])
```

`search` and `extract` take keyword arguments only; `search_queries` is
required for `search` and `urls` for `extract`. See
[`examples/search_and_extract.py`](examples/search_and_extract.py).

## Instrumented calls

| Call | Span name |
|---|---|
| `Parallel.search`, `AsyncParallel.search` | `parallel.search` |
| `Parallel.extract`, `AsyncParallel.extract` | `parallel.extract` |

Each call produces one span with `fi.span.kind` = `RETRIEVER`, including
calls through `with_raw_response`, `with_streaming_response`, `copy()` and
`with_options()`. Retries made by `parallel-web` inside one call stay in that
one span. The span is a child of the active span when there is one, and a
root span otherwise. It is current while `parallel-web` sends the request, so
a span from an HTTP client instrumentation nests under it.

## Span attributes

| Attribute | Calls | Value |
|---|---|---|
| `fi.span.kind` | all | `RETRIEVER` |
| `parallel.mode` | `search` with `mode` | The mode passed by the caller. Absent when not passed; the server default is not assumed. |
| `parallel.query_count` | `search`, `extract` with `search_queries` | Number of `search_queries` passed. |
| `gen_ai.retrieval.query`, `input.value` | `search`, `extract` with `search_queries` | The queries joined with a newline, Parallel API key replaced by `[redacted]`, cut to 1 KB of UTF-8 on a character boundary. |
| `parallel.url_count` | `extract` | Number of requested URLs. |
| `parallel.urls` | `extract` with `capture_urls=True` | See [Privacy](#privacy). |
| `parallel.objective` | `capture_objective=True` | See [Privacy](#privacy). |
| `parallel.result_count` | successful calls | Length of `results`. |
| `parallel.failed_url_count` | successful `extract` | Length of `errors` (requested URLs that were not extracted). The call itself still succeeded. |
| `parallel.search_id`, `parallel.extract_id` | successful calls | The vendor request id. |
| `parallel.session_id` | when passed or returned | The Parallel session id, from the request and then from the response. |
| `parallel.usage.names`, `parallel.usage.counts` | when the response has `usage` | Usage SKU names and their counts, in order. Never tokens or cost. |
| `parallel.warning_count` | when the response has `warnings` | Number of warnings. |
| `parallel.cancelled` | cancelled async calls | `true`; see [Errors and cancellation](#errors-and-cancellation). |

Each response warning becomes a `parallel.warning` span event with
`parallel.warning.type` and `parallel.warning.message` (key redacted, cut to
1 KB). At most 20 events are recorded per span. Warnings do not change the
span status.

A count, id or usage value that the response does not carry is omitted,
never written as 0. Calls through `with_raw_response` and
`with_streaming_response` return an unparsed HTTP response, so their spans
carry the request attributes and status only.

No model name, token count or cost is recorded: Parallel Search and Extract
do not return them, and `client_model` is not recorded.

## Privacy

Recorded by default: the search queries (redacted and capped as above),
counts, mode, ids, usage SKUs and warning text.

Never recorded: the Parallel API key, excerpts, titles, result URLs, full
page content, extract error content and warning `detail`. There is no
setting that adds them.

Not recorded unless you opt in, because they can carry tokens, personal data
or the full research goal:

```python
ParallelInstrumentor().instrument(
    tracer_provider=tracer_provider,
    capture_urls=True,        # parallel.urls: at most the first 20 requested URLs
    capture_objective=True,   # parallel.objective
)
```

Each captured URL and the objective has the API key replaced by `[redacted]`
and is cut to 1 KB of UTF-8. Both options must be booleans.

The API key is removed from every recorded value, including error messages
and stack traces. The package looks for it everywhere `parallel-web` keeps
it: `client.api_key` (also set from `PARALLEL_API_KEY`), the `x-api-key`
header in `default_headers` (or `PARALLEL_CUSTOM_HEADERS`), and a per-call
`extra_headers={"x-api-key": ...}`.

`TraceConfig` hide flags apply. Pass `config=TraceConfig(...)` or set the
environment variables before calling `instrument()`:

| Setting | Effect |
|---|---|
| `hide_inputs` / `FI_HIDE_INPUTS=true` | Drops `gen_ai.retrieval.query`, `input.value`, `parallel.urls` and `parallel.objective`. Counts, mode and ids stay. |
| `hide_outputs` / `FI_HIDE_OUTPUTS=true` | Drops `parallel.warning.message`. Warning types and counts stay. |

`config` must be a `fi_instrumentation.TraceConfig`; anything else raises
`TypeError` and nothing is wrapped.

Server-written text is recorded as the server wrote it, with only the key
removed: a warning message or an error message that quotes your request
shows that text even with `hide_inputs`. `hide_outputs` drops warning
messages; error messages and stack traces are always recorded on failed
calls.

## Errors and cancellation

An error raised by `parallel-web` (for example `AuthenticationError` for an
HTTP 401, `InternalServerError` for a 5xx, or `APIConnectionError`) sets the
span status to ERROR and records one `exception` event, with the API key
redacted from both. The exception is re-raised unchanged, so your code still
sees the server's original message.

After the key is removed, the error text is cut on a UTF-8 character
boundary: the status description's message and `exception.message` to 1 KB,
and `exception.stacktrace` to 16 KB.

Cancelling an `AsyncParallel` call (`asyncio.CancelledError`) ends the span
with status ERROR, description `cancelled`, and `parallel.cancelled` =
`true`, without an exception event.

A missing API key raises `ParallelError` in `Parallel()` or
`AsyncParallel()` itself, before any traced method runs, so no span is
recorded for it.

If the instrumentation itself fails (reading a request or response, starting
a span, recording an error), it logs at debug level and your call proceeds
with its own result or exception. `fi_instrumentation.suppress_tracing()`
skips the span.

## Limits

- Recorded text is cut on a UTF-8 character boundary, after the API key is
  removed: 1 KB for the joined queries, each captured URL, the objective,
  each warning message, `exception.message` and the message in the error
  status; 256 bytes for `parallel.mode`, ids, usage SKU names and warning
  types; 16 KB for `exception.stacktrace`. At most 20 URLs and 20 warning
  events are recorded per span.
- `parallel-web` copies `client.search` and `client.extract` into
  `client.with_raw_response` and `client.with_streaming_response` the first
  time either is read. A copy made before `instrument()` stays untraced;
  read them after instrumenting. A copy made while instrumented stops
  tracing after `uninstrument()`.
- `instrument()` checks the installed `parallel-web` version. Outside
  `>=1.0.1,<2` it logs an error and wraps nothing.
- `/v1beta/search` is not traced. No `parallel-web` 1.x method calls it.
- Do not combine this package with another instrumentor that wraps the
  same `parallel-web` methods, or calls will produce duplicate spans.
