# traceAI-exa

OpenTelemetry instrumentation for the [`exa-py`](https://github.com/exa-labs/exa-py) client.

This package wraps Exa client calls; it is not an Exa exporter. Exa does not publish an exporter.

## Installation

```bash
pip install traceAI-exa
```

It accepts `exa-py>=2.25.0,<3` and is tested with `exa-py` 2.25.0 on Python
3.10, 3.11, 3.12 and 3.13. The test suite also passes on `exa-py` 2.26.0.

## Configure and trace

Set your Future AGI and Exa credentials in your application environment:

```bash
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
export EXA_API_KEY="your-exa-api-key"
```

```python
import os
from exa_py import Exa
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_exa import ExaInstrumentor

tracer_provider = register(project_name="exa-search", project_type=ProjectType.OBSERVE)
ExaInstrumentor().instrument(tracer_provider=tracer_provider)

client = Exa(api_key=os.environ["EXA_API_KEY"])
client.search("recent retrieval research", num_results=5)
tracer_provider.force_flush()
```

Open the `exa-search` project in Future AGI's Observe view and select the
`exa.search` span. By default, it shows the query, result count, duration,
status, and cost when Exa reports one. Enable response previews below to
inspect retrieved content.

## Instrumented calls

`Exa` and `AsyncExa` calls to `search`, `get_contents`, `answer`, and the
deprecated `search_and_contents` alias (traced as `exa.search`) each produce
one span. `stream_search` and `stream_answer` produce one span that stays open
until the stream is fully read, closed, or garbage-collected; the returned
object is still an instance of the vendor's stream class. A stream still open
when the interpreter exits is ended as cancelled before `register()`'s exit
hook shuts the exporter down (when `instrument()` runs after `register()`).

`gen_ai.span.kind` is `RETRIEVER` for `search`, `answer` and the two streams,
and `TOOL` for `get_contents`, which fetches the pages you name rather than
searching. Each span is a child of the active span
when there is one, and a root span otherwise. The span is current while
exa-py sends the request, so a span from an HTTP client instrumentation nests
under it.

## Span attributes

| Attribute | Calls | Value |
|---|---|---|
| `gen_ai.retrieval.query`, `input.value` | `search`, `answer`, streams | The query, with the client's Exa API key replaced by `[redacted]`, cut to 1 KB of UTF-8 on a character boundary. |
| `exa.url_count` | `get_contents` | Number of requested URLs (a single URL, a list of URLs, or a list of results). |
| `exa.urls` | `get_contents` with `capture_urls=True` | See [get_contents URLs](#get_contents-urls). |
| `exa.result_count` | successful calls | `results` for `search` and `get_contents`, `citations` for `answer`, and the citations summed over every chunk of a fully read stream. Absent when the count is unknown (an error or a cancellation), never 0 for unknown. |
| `exa.cancelled` | cancelled calls and streams | `true`; see [Errors and cancellation](#errors-and-cancellation). |

Response content is off by default. The instrumentor never invents a model
name or token usage. The client's Exa key is replaced by `[redacted]` wherever
it appears verbatim in recorded text.

### Response previews

Opt in to content capture when you first instrument the client:

```python
ExaInstrumentor().instrument(
    tracer_provider=tracer_provider,
    capture_outputs=True,
    capture_urls=True,  # Optional: also include returned document URL fields.
)
```

`output.value` is a JSON preview (`output.mime_type=application/json`):

- `search`, `search_and_contents`, and `get_contents`: `results` containing
  available titles, text, summaries, and highlights.
- `answer`: answer text and `citations` containing available titles and text.
  A structured answer is serialized into a bounded text preview.
- Fully consumed streams: accumulated text in `answer` and returned `citations`.
  A failed or cancelled stream does not export partial output.

Previews include at most 20 documents or citations, 10 highlights per document,
1 KiB of UTF-8 per document field or highlight, and 8 KiB of answer text. The
entire JSON preview is capped at 16 KiB; trailing documents or answer text may
be omitted to fit. `exa.output_truncated=true` marks a shortened preview.
`exa.result_count` still describes the full response, and your returned objects
and stream chunks are unchanged. Structured answers also have depth and item
limits; previews are not a replacement for storing application results.

Returned URL fields require both `capture_outputs=True` and `capture_urls=True`.
Document IDs are not copied because Exa IDs can themselves contain URLs.
`capture_urls` controls URL fields, not links embedded in captured text.

`FI_HIDE_OUTPUTS=true` or `TraceConfig(hide_outputs=True)` overrides this opt-in
and prevents response previews. `hide_output_text=True` also disables previews.
`hide_inputs=True` suppresses URL fields and removes verbatim request echoes
from captured output. `pii_redaction=True` applies before text is serialized.

### Reported cost

When Exa returns `cost_dollars.total`, `gen_ai.cost.total` records that amount
in USD, independently of content capture. Available breakdowns use
`exa.cost.search.neural`, `exa.cost.search.keyword`, `exa.cost.contents.text`,
and `exa.cost.contents.summary`. These are vendor request charges, not inferred
LLM input/output token costs. Only finite, non-negative numbers are recorded;
a missing cost stays absent, while a reported zero stays zero. Streaming chunks
do not expose cost metadata, so stream cost is absent.

### get_contents URLs

`get_contents` spans record the number of requested URLs, not the URLs,
because URLs can carry tokens or personal data. To record the URLs as well,
opt in when instrumenting:

```python
ExaInstrumentor().instrument(tracer_provider=tracer_provider, capture_urls=True)
```

With `capture_urls=True`, `exa.urls` holds at most the first 20
requested URLs, each with the Exa API key replaced by `[redacted]` and cut to
1 KB. Query strings are otherwise kept as given. Hidden inputs (below) turn
URL capture off.

## Trace config and context

Spans are created through `fi_instrumentation`'s `FITracer`, as in the other
traceAI instrumentors. Pass a `TraceConfig` to `instrument()`, or set the
matching `FI_*` environment variables; without `config`, the environment is
read when `instrument()` runs.

```python
from fi_instrumentation import TraceConfig

ExaInstrumentor().instrument(
    tracer_provider=tracer_provider, config=TraceConfig(hide_inputs=True)
)
```

With `hide_inputs=True` (or `FI_HIDE_INPUTS=true`), `input.value` is
`__REDACTED__`, `gen_ai.retrieval.query` and `exa.urls` are not recorded,
and the query or the requested URLs are replaced by `__REDACTED__` in error
text. The URL and result counts stay. `pii_redaction` applies to the recorded
attributes and to the error text described below, and
`using_session`, `using_user`, `using_metadata` and `using_tags` attributes are
added to the Exa spans.

## Errors and cancellation

An error raised by exa-py (for example `ValueError` for an HTTP 4xx/5xx
response) sets the span status to ERROR and records an exception event. The
exception is re-raised unchanged.

exa-py puts the server's response body in its error message, and a body can
repeat the request. On the span, the status description
(`<ExceptionType>: <message>`) and the event's `exception.message` therefore
have the Exa key replaced by `[redacted]` (and, with hidden inputs, the query
or the requested URLs by `__REDACTED__`; with `pii_redaction`, PII by tokens such
as `<EMAIL_ADDRESS>`) and the message cut to 1 KB of UTF-8.
If the exception's own `str()` fails, the span records only its type. The event carries
`exception.type` and `exception.message` only: no `exception.stacktrace` is
exported, because frames hold local file paths, user names and host names.
Chained exceptions' messages are not copied. Only verbatim
copies are found: an escaped, normalised or truncated echo of the key, query or
URLs (for example a JSON-escaped `\/` or a percent-encoded URL) is not.

Closing a stream before its last chunk (`close()` or `aclose()`), dropping it
mid-iteration, or cancelling an `AsyncExa` call or stream ends the span with
status ERROR, description `cancelled`, and `exa.cancelled` = `true`.

A traced `AsyncExa` stream always has `aclose()`, which ends the span and closes
the HTTP response. exa-py's own async streams have `aclose()` from 2.26.0 only;
2.25.0's have none. On both versions their `close()` raises `RuntimeError`
(traced or not; the span is ended first). Code that must also run on exa-py
2.25.0 without this instrumentor can call `aclose()` only when it exists, for
example `aclose = getattr(stream, "aclose", None)`.

A missing API key raises `ValueError` in `Exa()` or `AsyncExa()` itself,
before any traced method runs, so no span is recorded for it.

`find_similar` and `find_similar_and_contents` are deprecated upstream and
are not traced. Use `client.search("pages similar to " + url)` instead; pass
`contents={"text": True}` when you need page text.

Do not use this package together with another instrumentor that wraps the same
Exa client methods, or duplicate spans may result.
