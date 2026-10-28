# traceAI-tavily

OpenTelemetry instrumentation for the bare [`tavily-python`](https://github.com/tavily-ai/tavily-python)
client: `TavilyClient` and `AsyncTavilyClient`, `search` and `extract`.

This package wraps Tavily client calls. It is not a Tavily exporter; Tavily does
not publish one. It exists because a measurement showed the bare client emits
nothing on its own (see [What Tavily already emits](#what-tavily-already-emits)).
Alpha: validated against a local test server and a local Future AGI deployment, and in a live run against api.tavily.com on 2026-10-17 (see [Live vendor verification](#live-vendor-verification-2026-10-17)).

## Installation

```bash
pip install traceAI-tavily
```

It accepts `tavily-python>=0.8.4,<1` and `fi-instrumentation-otel>=1.1.0`, and is
tested with `tavily-python` 0.8.4 and 0.8.5 (the latest release on 2026-10-16;
0.8.5 only renames the `max_hours` argument of `search` to `max_age_hours`; this
package reads the query by name, so it is unaffected) on Python 3.10, 3.11, 3.12 and 3.13. The 0.8.5 test run and the
local Future AGI end-to-end run were on Python 3.12.

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
calls `TavilyClient`. The current recommended tool, `langchain_tavily.TavilySearch`
(`langchain-tavily` 0.2.18; the Tavily docs mark the community tool as deprecated),
was run live on 2026-10-17 with this package instrumented: it returned results
and this package produced 0 spans, so it needs `traceAI-langchain` too.

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
| `TavilyClient` / `AsyncTavilyClient` `search` + `extract`, `register()` only (also as the global provider), with or without `traceAI-langchain` | 0 spans for 4 calls | 1 span per call (`search` RETRIEVER, `extract` TOOL) |
| LangGraph `ToolNode` running `TavilySearchResults`, `traceAI-langchain` on | 1 TOOL span | 1 TOOL span (unchanged) |
| Your LangChain tool calling `TavilyClient.search` | 1 TOOL span | 2 spans: the TOOL span and a RETRIEVER `tavily.search` (see above) |

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
| `gen_ai.span.kind` | all | `RETRIEVER` for `search`; `TOOL` for `extract`, which fetches the pages it is given. |
| `gen_ai.tool.name` | all | `tavily.search` or `tavily.extract` |
| `input.value` | `search`; `extract` when `query` is given | The query, with the client's Tavily key replaced by `[redacted]`, then cut to 1024 bytes of UTF-8 on a character boundary. |
| `gen_ai.retrieval.query` | `search` | The same text as `input.value`. Not recorded when inputs are hidden. |
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
  `__REDACTED__` and drops `gen_ai.retrieval.query`. It also removes the query text from error messages: after the
  key is redacted, each verbatim occurrence of the query in the error status
  description and the exception event's message and stacktrace becomes
  `__REDACTED__`. Other error text is recorded as thrown, with the key
  redacted.
- **PII**: `FI_PII_REDACTION=true`, or `TraceConfig(pii_redaction=True)`,
  replaces emails, phone numbers and similar with tokens such as
  `<EMAIL_ADDRESS>` in every attribute and, after the key (and a hidden query)
  is removed, in the error status and the exception event. `config` must be a `fi_instrumentation.TraceConfig`; anything else
  raises `TypeError`.

## Errors and cancellation

An exception from the call (tested with `InvalidAPIKeyError`,
`BadRequestError`, `TavilyKeylessLimitError`, `requests.HTTPError` and
`httpx.HTTPStatusError`) sets the span status to ERROR with
`<ExceptionType>: <message>` (key redacted, the query too when inputs are
hidden, 1 KB cap), records one exception event, and is re-raised unchanged.
The exception event never carries the raw traceback (it would hold local file
paths, the username and code frames): `exception.stacktrace` is the same
frames-free `<ExceptionType>: <message>` summary as the status, and
`exception.type` is the class name. The caller still gets the original
exception with its own traceback.
There is no retry.

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

## Keyless clients

tavily-python 0.8.5 lets `TavilyClient()` run with no key (`search` and
`extract` only; past the free allowance it raises `TavilyKeylessLimitError`).
The wrapper traces these calls like keyed ones: there is no key to redact, the
query is recorded (or hidden with `FI_HIDE_INPUTS`), and a limit error is an
ERROR span. Covered by tests and by one live keyless search. The `project_id`
and session header options on the client are not read or recorded.

## Live vendor verification (2026-10-17)

Real `api.tavily.com` with a Future AGI-owned key, `tavily-python` 0.8.5,
Python 3.12, exporting through `register()` to a local Future AGI: an agent span
with one `search` (RETRIEVER, 1 result), one `extract` (TOOL, 1 URL, 0 failed),
one keyless `search`, and one call with an invalid key (ERROR,
`InvalidAPIKeyError: Unauthorized: missing or invalid API key`). `fi_verify`
passed (1 trace, 5 spans); the same run on the pinned 0.8.4 (no keyless client) passed with 4 spans and the same scan. Scanning every stored column of the 5 rows (including
span events, resource and other attributes): the Tavily key, `tvly-`, the
response text and response URL, `Traceback`, `File "`, `/Users/`, the username
and the hostname appear in 0 rows. A unique query marker appears in 3 of 5 rows
by default and in 0 of 5 with `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true`.
Future AGI stores an exception event's name and time only, so the exported
event attributes were checked separately on the span objects an exporter
receives (`tests/test_privacy.py`, and a live bad-key call: the event holds
`InvalidAPIKeyError: Unauthorized: missing or invalid API key.` and no frames).

## Limits

- Only `search` and `extract`; see [Instrumented calls](#instrumented-calls).
- No usage, credits, request id or search parameters (depth, topic, domains)
  are recorded.
- Live verification was one search, one extract, one keyless search and one
  bad-key call (below); no load or long-running test.

## Tests

Every Tavily call goes to a loopback fake and every key is a placeholder.

As for the other traceAI Python packages, install the package and run `pytest`
in its directory (no `PYTHONPATH` needed; the tests that use the shared harness
in `python/tests/harness` add it to `sys.path` themselves). This tests against
the published `fi-instrumentation-otel`:

```bash
cd python/frameworks/tavily
pip install -e . pytest
pytest
```

The two commands below instead run from the repository root against the
`fi_instrumentation` source in this checkout, with pinned versions. Run them
with any `PYTHONPATH` of your own unset; they set their own.

Base suite:

```bash
PYTHONPATH="python/frameworks/tavily:python:python/tests" uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api --with opentelemetry-sdk \
  --with opentelemetry-instrumentation --with opentelemetry-exporter-otlp-proto-http \
  --with wrapt --with requests --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'tavily-python==0.8.4' \
  pytest python/frameworks/tavily/tests -q -p no:cacheprovider --noconftest -o addopts= -rs
```

With the LangChain extras:

```bash
PYTHONPATH="python/frameworks/tavily:python:python/tests" uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api --with opentelemetry-sdk \
  --with opentelemetry-instrumentation --with opentelemetry-exporter-otlp-proto-http \
  --with 'wrapt<2' --with requests --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'tavily-python==0.8.4' --with langchain-community==0.4.2 --with langchain-core==1.5.2 \
  --with langgraph==1.2.2 \
  pytest python/frameworks/tavily/tests -q -p no:cacheprovider --noconftest -o addopts= -rs
```

The base command (and plain `pytest` without the LangChain packages installed)
skips the 7 measurement tests that run LangChain or LangGraph (the AC-01 and
AC-04 LangGraph runs, the bare-client run with traceAI-langchain on, and the two
custom-tool runs, all in `tests/test_measurement.py`); the
second command adds the extras they need and runs them. It pins `wrapt<2`
(`fi-instrumentation-otel` declares `wrapt ^1.15.0`); the base command takes
the newest `wrapt`. `--python` can be 3.10, 3.11, 3.12 or 3.13.

These measurement tests live in this package, not under
`python/frameworks/langchain/`. That is a deviation from PRD section 13, made to
keep the change in one package.
