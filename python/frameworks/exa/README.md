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

Do not use this package together with another instrumentor that wraps the same
Exa client methods, or duplicate spans may result.
