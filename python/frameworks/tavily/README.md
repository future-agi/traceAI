# traceAI-tavily

Trace `TavilyClient` and `AsyncTavilyClient` search, extraction, mapping,
crawling and research calls in Future AGI.

## Install

Requires Python 3.10–3.13, `tavily-python>=0.8.4,<1`, and
`fi-instrumentation-otel>=1.1.0`.

```bash
pip install traceAI-tavily
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

## Trace research

Use the same instrumented client to create a research task and fetch its status.
Each client call has its own span; `tavily.request_id` links calls for the same task.

```python
task = client.research(input="Explain the main OpenTelemetry signals", model="mini")
result = client.get_research(task["request_id"])
tracer_provider.force_flush()
```

For streaming research, consume the returned iterator and close it if you stop
early. The span stays open until exhaustion, an exception, or an explicit close.
It is current only during SDK work, not while your code processes a chunk.

```python
stream = client.research(input="Explain distributed tracing", model="mini", stream=True)
try:
    for chunk in stream:
        print(chunk.decode("utf-8", errors="replace"), end="")
finally:
    stream.close()
    tracer_provider.force_flush()
```

For `AsyncTavilyClient`, await the method, consume research with `async for`, and
use `await stream.aclose()` in `finally`. Always close the async client with
`await client.close()`. An unconsumed stream must still be explicitly closed;
do not rely on garbage collection to export it.

## Supported calls

Both client classes support these spans. The deprecated `tavily.Client`
subclass inherits the synchronous instrumentation.

| Method | Span | Kind |
| --- | --- | --- |
| `search` | `tavily.search` | `RETRIEVER` |
| `extract` | `tavily.extract` | `TOOL` |
| `map` | `tavily.map` | `TOOL` |
| `crawl` | `tavily.crawl` | `TOOL` |
| `research` | `tavily.research` | `TOOL` |
| `get_research` | `tavily.get_research` | `TOOL` |

Only these public methods are wrapped. Private HTTP helpers, `feedback`,
`get_search_context`, `qna_search` and `get_company_info` have no separate spans.
If a convenience method calls a supported method, that underlying call is traced
once. Usage, credits, search options and server-side research progress are not
recorded. A successful `research` span means the SDK call or byte stream completed,
not that an asynchronous research job has finished.

## Privacy and attributes

- Search queries, extract reranking queries and research prompts are recorded
  as `input.value`, with client keys redacted and text capped at 1,024 UTF-8 bytes.
  Search also records `gen_ai.retrieval.query`.
- Result bodies, research output, URL arguments, crawl instructions and schemas
  are not recorded. `extract`, `map` and `crawl` record URL counts instead.
  Text you put into a query or prompt is still input text; hide inputs if it is sensitive.
- `tavily.result_count` records the number of results when present;
  `tavily.failed_result_count` records failed extractions. Unknown counts are omitted.
- Mapping, crawling and research record `tavily.request_id` when available.
  Streaming research reads only a bounded SSE `data:` line's top-level ID;
  malformed or oversized IDs are omitted. Payload bytes reach your code unchanged.
- Set `FI_HIDE_INPUTS=true` to mask inputs, omit retrieval queries and research
  identifiers, and remove search/extract query text from error details.
- Set `FI_PII_REDACTION=true` to redact supported personal-data patterns.
  You can also pass `config=TraceConfig(hide_inputs=True, pii_redaction=True)`
  to `instrument()`, importing `TraceConfig` from `fi_instrumentation`.

Errors are re-raised unchanged. New mapping, crawling and research spans record
only the exception type, never the error message or traceback. Search/extract
retain scrubbed error summaries, without traceback frames. Cancelling an async
call or stream sets ERROR and `tavily.cancelled=true`, without an exception event.
Closing a stream early sets `tavily.stream.closed=true`; it does not assert successful
exhaustion. Instrumentation failures do not replace your SDK result or exception.

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
- **Research span still open:** exhaust or close its iterator. `research(stream=True)`
  is lazy for the async client; returning the iterator is not completion.
- **Keyless research fails:** Tavily's keyless mode supports search/extract, not
  research, mapping or crawling. Supply `TAVILY_API_KEY` for those operations.
- **Stop tracing:** `TavilyInstrumentor().uninstrument()` restores the original
  client methods; subsequent calls are not traced.
