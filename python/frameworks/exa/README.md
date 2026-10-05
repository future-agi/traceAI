# traceAI-exa

OpenTelemetry instrumentation for the [`exa-py`](https://github.com/exa-labs/exa-py) client.

This package wraps Exa client calls; it is not an Exa exporter. Exa does not publish an exporter.

## Installation

```bash
pip install traceAI-exa
```

It accepts `exa-py>=2.25.0,<3` and is tested with `exa-py` 2.25.0 on Python
3.10, 3.11 and 3.13.

## Usage

```python
from exa_py import Exa
from fi_instrumentation import register
from traceai_exa import ExaInstrumentor

tracer_provider = register(project_name="exa-search")
ExaInstrumentor().instrument(tracer_provider=tracer_provider)

client = Exa(api_key="your-exa-api-key")
client.search("recent retrieval research", num_results=5)
```

## Instrumented calls

`Exa` and `AsyncExa` calls to `search`, `get_contents`, `answer`, and the
deprecated `search_and_contents` alias (traced as `exa.search`) each produce
one span. `stream_search` and `stream_answer` produce one span that stays open
until the stream is fully read, closed, or garbage-collected; the returned
object is still an instance of the vendor's stream class.

Every span has `fi.span.kind` = `RETRIEVER`. It is a child of the active span
when there is one, and a root span otherwise. The span is current while
exa-py sends the request, so a span from an HTTP client instrumentation nests
under it.

## Span attributes

| Attribute | Calls | Value |
|---|---|---|
| `fi.retrieval.query`, `input.value` | `search`, `answer`, streams | The query, with the client's Exa API key replaced by `[redacted]`, cut to 1 KB of UTF-8 on a character boundary. |
| `fi.retrieval.url_count` | `get_contents` | Number of requested URLs (a single URL, a list of URLs, or a list of results). |
| `fi.retrieval.urls` | `get_contents` with `capture_urls=True` | See [get_contents URLs](#get_contents-urls). |
| `fi.retrieval.document_count` | successful calls | `results` for `search` and `get_contents`, `citations` for `answer`, and the citations summed over every chunk of a fully read stream. Absent when the count is unknown (an error or a cancellation), never 0 for unknown. |
| `exa.cancelled` | cancelled calls and streams | `true`; see [Errors and cancellation](#errors-and-cancellation). |

The spans never carry response content: no result titles, text, highlights or
summaries, no answer or stream text, no cost, token count or model name. There
is no setting that adds them. The Exa API key is never recorded; it is
replaced by `[redacted]` wherever it appears in a recorded query or URL.

### get_contents URLs

`get_contents` spans record the number of requested URLs, not the URLs,
because URLs can carry tokens or personal data. To record the URLs as well,
opt in when instrumenting:

```python
ExaInstrumentor().instrument(tracer_provider=tracer_provider, capture_urls=True)
```

With `capture_urls=True`, `fi.retrieval.urls` holds at most the first 20
requested URLs, each with the Exa API key replaced by `[redacted]` and cut to
1 KB. Query strings are otherwise kept as given.

## Errors and cancellation

An error raised by exa-py (for example `ValueError` for an HTTP 4xx/5xx
response) sets the span status to ERROR and records an exception event. The
exception is re-raised unchanged.

Closing a stream before its last chunk (`close()` or `aclose()`), dropping it
mid-iteration, or cancelling an `AsyncExa` call or stream ends the span with
status ERROR, description `cancelled`, and `exa.cancelled` = `true`. On an
`AsyncExa` stream, prefer `await stream.aclose()`: it ends the span and closes
the HTTP response. exa-py's own `close()` on an async stream raises
`RuntimeError` from httpx; the span is ended first and that behaviour is left
as it is.

A missing API key raises `ValueError` in `Exa()` or `AsyncExa()` itself,
before any traced method runs, so no span is recorded for it.

Do not use this package together with another instrumentor that wraps the same
Exa client methods, or duplicate spans may result.
