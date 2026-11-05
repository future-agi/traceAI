# traceAI-tavily

Trace `TavilyClient` and `AsyncTavilyClient` search and extraction calls in
Future AGI.

## Install

Requires Python 3.10–3.13, `tavily-python>=0.8.4,<1`, and
`fi-instrumentation-otel>=1.1.0`.

```bash
pip install "traceAI-tavily>=0.1.0"
```

Set `FI_API_KEY`, `FI_SECRET_KEY` and `TAVILY_API_KEY` in your environment.
For a self-hosted Future AGI deployment, also set `FI_BASE_URL` to its base URL.

## Trace a search

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from tavily import TavilyClient
from traceai_tavily import TavilyInstrumentor

tracer_provider = register(
    project_name="tavily-example", project_type=ProjectType.OBSERVE, verbose=False
)
TavilyInstrumentor().instrument(tracer_provider=tracer_provider)

client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
response = client.search("What is OpenTelemetry?", max_results=2)
print("results:", len(response["results"]))
tracer_provider.force_flush()
```

Open the `tavily-example` project in Future AGI to find the `tavily.search`
span. Calls made inside an active OpenTelemetry span appear as its children.
The instrumentor also supports `using_session`, `using_user` and
`suppress_tracing` from `fi_instrumentation`.

## Supported calls

Both client classes support these spans. With `AsyncTavilyClient`, await the
method and close the client with `await client.close()` when finished.
The deprecated `tavily.Client` subclass inherits the synchronous instrumentation.

| Method | Span | Kind |
| --- | --- | --- |
| `search` | `tavily.search` | `RETRIEVER` |
| `extract` | `tavily.extract` | `TOOL` |

Private HTTP helpers and convenience methods have no separate spans. If a
convenience method calls a supported method, that underlying call is traced
once. Usage, credits and search options are not recorded.

## Privacy and attributes

- Search queries and extract reranking queries are recorded as `input.value`,
  with client keys redacted and text capped at 1,024 UTF-8 bytes.
  Search also records `gen_ai.retrieval.query`.
- Result bodies and URL arguments are not recorded. Extraction records URL
  counts instead. Text you put into a query is still input text; hide inputs
  if it is sensitive.
- `tavily.result_count` records the number of results when present;
  `tavily.failed_result_count` records failed extractions. Unknown counts are omitted.
- Set `FI_HIDE_INPUTS=true` to mask inputs, omit retrieval queries and remove
  search/extract query text from error details.
- Set `FI_PII_REDACTION=true` to redact supported personal-data patterns before
  query text is capped, so truncation cannot leave a recognizable PII fragment.
  You can also pass `config=TraceConfig(hide_inputs=True, pii_redaction=True)`
  to `instrument()`, importing `TraceConfig` from `fi_instrumentation`.

Errors are re-raised unchanged. Search HTTP/transport failures record only the
exception type, never messages or tracebacks: these errors can include custom
endpoint credentials. Other search errors retain scrubbed, frames-free summaries.
Extraction failures also record only the type, because errors can echo sensitive
URL arguments. Type-only recording applies even with default privacy settings
and on the minimum supported core version. Cancelling an async call sets ERROR
and `tavily.cancelled=true`, without an exception event. Instrumentation failures do not replace your SDK
result or exception.

## LangChain and LangGraph

For `langchain_tavily.TavilySearch` or the community Tavily tool, use
[`traceAI-langchain`](../langchain) to trace the tool call. This package targets
bare Tavily clients, not those tool wrappers.

If your own LangChain tool calls `TavilyClient`, enabling both instrumentors
creates a tool span and a lower-level client span. Enable only LangChain tracing
if you want one span per tool call. To hide tool results that become a later
agent input, set both `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true`.

## Troubleshooting

- **No spans:** instrument before calling the client, pass the provider returned
  by `register()`, and flush before a short-lived process exits. Check the
  `traceai_tavily` logger for an unsupported SDK version or a moved method.
- **Stop tracing:** `TavilyInstrumentor().uninstrument()` restores the original
  client methods; subsequent calls are not traced.
