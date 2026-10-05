# traceAI-tavily

OpenTelemetry instrumentation for the bare [`tavily-python`](https://github.com/tavily-ai/tavily-python)
client: `TavilyClient` and `AsyncTavilyClient`, `search` and `extract`.

This package wraps Tavily client calls. It is not a Tavily exporter; Tavily does
not publish one. It exists because a measurement showed the bare client emits
nothing on its own (see [What Tavily already emits](#what-tavily-already-emits)).
Alpha: validated against a local test server, never against api.tavily.com.

## Installation

```bash
pip install traceAI-tavily
```

It accepts `tavily-python>=0.8.4,<1` and `fi-instrumentation-otel>=1.1.0`, and is
tested with `tavily-python` 0.8.4 on Python 3.10, 3.11, 3.12 and 3.13.

## Which path do you use?

Tavily reaches an application in two ways. They are different integrations.

### LangChain tool: use traceAI-langchain, not this package

`langchain_community.tools.tavily_search.TavilySearchResults` (the tool in the
LangGraph examples under `python/frameworks/langchain/examples/`) is already
traced by `traceAI-langchain`. Measured with a LangGraph `ToolNode` running it:
one span per tool call, `gen_ai.span.kind` = `TOOL`, named
`tavily_search_results_json`, with `gen_ai.tool.name`, `input.value` (the query)
and `output.value` (the tool's result text).

Installing this package as well adds no span to that path:
`TavilySearchResults` posts to the Tavily API with `requests` itself and never
calls `TavilyClient`. (`langchain-tavily` 0.2.18 does the same, from reading its
source; that package was not run here.)

`traceAI-langchain` exports the result text by default. `FI_HIDE_OUTPUTS=true`
masks the tool span's `output.value`, but the text is then the next agent turn's
input and still leaves through that span's `input.value`. Only
`FI_HIDE_INPUTS=true` together with `FI_HIDE_OUTPUTS=true` kept it in the
process in the measurement.

### Bare client: use this package

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from tavily import TavilyClient
from traceai_tavily import TavilyInstrumentor

tracer_provider = register(project_name="my-agent", project_type=ProjectType.OBSERVE)
TavilyInstrumentor().instrument(tracer_provider=tracer_provider)

client = TavilyClient(api_key="tvly-...")
client.search("What is OpenTelemetry?")
```

`examples/search.py` is the same script; the test suite runs it.

### A LangChain tool you wrote over TavilyClient

If your own LangChain tool calls `TavilyClient.search` and both instrumentors
are on, one tool call gives two spans: the LangChain tool span and a
`tavily.search` span. `tavily.search` is a child of whatever OpenTelemetry span
is current when the client is called, and starts its own trace only when no
span is current. `traceAI-langchain` does not make its tool span current, so
`tavily.search` is not a child of it. To make it one, make the tool span current
around the call:

```python
from opentelemetry import trace
from traceai_langchain import get_current_span

with trace.use_span(
    get_current_span(),
    end_on_exit=False,
    record_exception=False,
    set_status_on_exception=False,
):
    response = client.search(query)
```

`record_exception=False, set_status_on_exception=False` leave the tool span's
error to `traceAI-langchain`, which records it once; without them a failing
call records the same exception twice on the tool span. The `tavily.search`
span records its own exception event either way.

If you do not want the second span, do not instrument Tavily in that process.

## What Tavily already emits

`tests/test_measurement.py` runs both paths against a local Tavily fake and
exports through `register()` to a local collector receiver (tavily-python 0.8.4,
langchain-community 0.4.2, langchain-core 1.5.2, langgraph 1.2.2):

| Path | Without traceAI-tavily | With traceAI-tavily |
| --- | --- | --- |
| `TavilyClient` / `AsyncTavilyClient` `search` + `extract`, `register()` only (also as the global provider), with or without `traceAI-langchain` | 0 spans for 4 calls | 1 TOOL span per call |
| LangGraph `ToolNode` running `TavilySearchResults`, `traceAI-langchain` on | 1 TOOL span | 1 TOOL span (unchanged) |
| Your LangChain tool calling `TavilyClient.search` | 1 TOOL span | 2 spans (see above) |

tavily-python 0.8.4 contains no OpenTelemetry code.

## Instrumented calls

| Call | Span |
| --- | --- |
| `TavilyClient.search`, `AsyncTavilyClient.search` | `tavily.search` |
| `TavilyClient.extract`, `AsyncTavilyClient.extract` | `tavily.extract` |

The deprecated `tavily.Client` subclass is covered through `TavilyClient`.
`crawl`, `map`, `research`, `get_research`, `feedback`, `get_search_context`,
`qna_search` and `get_company_info` are not traced.

Each span is a child of the active span, or a root span when there is none. It
is current while tavily-python sends the request, so a span from an HTTP client
instrumentation nests under it.

## Span attributes

| Attribute | Calls | Value |
| --- | --- | --- |
| `gen_ai.span.kind` | all | `TOOL` |
| `gen_ai.tool.name` | all | `tavily.search` or `tavily.extract` |
| `input.value` | `search`; `extract` when `query` is given | The query, with the client's Tavily key replaced by `[redacted]`, then cut to 1024 bytes of UTF-8 on a character boundary. |
| `tavily.url_count` | `extract` | Number of requested URLs (a single URL string counts as 1). |
| `tavily.result_count` | successful calls | Length of the response's `results`. |
| `tavily.failed_result_count` | successful `extract` | Length of the response's `failed_results`. |
| `tavily.cancelled` | cancelled `AsyncTavilyClient` calls | `true` |

A count is absent when it is unknown (an error, a cancellation, or a response
whose list is missing or not a list); it is never written as 0 for unknown. An
`extract` with some failed URLs is still a successful call.

The query is read by its parameter name, `query`, from the method's signature
(read once per method), so it does not depend on argument positions. If the
arguments do not fit the signature, no `input.value` is recorded; the call
still goes to tavily-python unchanged.

Context attributes from `fi_instrumentation` (`using_session`, `using_user`,
...) are added to the span; `session.id` and `user.id` are tested.
`suppress_tracing()` records nothing.

## Privacy

- **Response content is never recorded**: no result titles, content, raw
  content, answers, images or extracted pages. There is no setting that adds it.
- **URLs are never recorded**: `extract` records only how many URLs were
  requested, because URLs can carry tokens or personal data.
- **The Tavily key is never recorded.** It is removed from every recorded text
  (`input.value`, the error status and the exception event's message and
  stacktrace) wherever tavily-python keeps it: `TavilyClient.api_key`,
  `TavilyClient.headers["Authorization"]`, the requests session's
  `Authorization` header (including a session you passed in), and the httpx
  client's `Authorization` header for `AsyncTavilyClient` (which has no
  `api_key` attribute). If one of those places cannot be read, no free text is
  recorded for that call: no `input.value`, the exception type as the error
  description, and `[redacted]` as the exception message.
- **Hiding queries**: `FI_HIDE_INPUTS=true`, or
  `instrument(config=TraceConfig(hide_inputs=True))`, records `input.value` as
  `__REDACTED__`. `config` must be a `fi_instrumentation.TraceConfig`; anything
  else raises `TypeError`.

## Errors and cancellation

An exception from the call (tested with `InvalidAPIKeyError`,
`BadRequestError`, `TavilyKeylessLimitError`, `requests.HTTPError` and
`httpx.HTTPStatusError`) sets the span status to ERROR with
`<ExceptionType>: <message>` (key redacted, 1 KB cap), records one exception
event, and is re-raised unchanged. There is no retry.

Cancelling an `AsyncTavilyClient` call ends the span with status ERROR,
description `cancelled`, and `tavily.cancelled` = `true`, without an exception
event. A `TavilyClient` call cannot be cancelled.

A failure inside the instrumentation (reading arguments, starting or ending the
span, reading the result) is logged at debug level on the `traceai_tavily`
logger and never changes what the call returns or raises; if the span cannot
start, the call runs untraced.

## Instrumentor behaviour

- `uninstrument()` puts the original methods back; later calls are not traced.
- `instrument()` checks the installed `tavily-python` against `>=0.8.4,<1`; on
  another release it logs the conflict and wraps nothing (OpenTelemetry's
  `BaseInstrumentor` behaviour).
- If a tavily-python release moves `TavilyClient`, `AsyncTavilyClient` or one of
  the two methods, `instrument()` logs a WARNING naming it, skips it, and
  instruments the rest.

## Limits

- Only `search` and `extract`; see [Instrumented calls](#instrumented-calls).
- No usage, credits, request id or search parameters (depth, topic, domains)
  are recorded.
- Validated against a local Tavily fake only; no call to api.tavily.com was
  made.
