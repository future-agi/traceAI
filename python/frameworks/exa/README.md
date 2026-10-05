# traceAI-exa

OpenTelemetry instrumentation for the [`exa-py`](https://github.com/exa-labs/exa-py) client.

This package wraps Exa client calls; it is not an Exa exporter. Exa does not publish an exporter.

## Installation

```bash
pip install traceAI-exa
```

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
deprecated `search_and_contents` alias are traced. `stream_search` and
`stream_answer` remain open for the life of their iterators.

Each span records only the request query (limited to 1024 characters) and the
returned document count. Content capture is off by default: highlights, text,
and response bodies are not added to span attributes. API keys are never added
to span attributes.

`get_contents` spans record the number of requested URLs
(`fi.retrieval.url_count`), not the URLs, because URLs can carry tokens or
personal data. To record the URLs as well, opt in when instrumenting:

```python
ExaInstrumentor().instrument(tracer_provider=tracer_provider, capture_urls=True)
```

With `capture_urls=True`, `fi.retrieval.urls` holds at most the first 20
requested URLs, each with the Exa API key replaced by `[redacted]` and cut to
1 KB. Query strings are otherwise kept as given.

Do not use this package together with another instrumentor that wraps the same
Exa client methods, or duplicate spans may result.
