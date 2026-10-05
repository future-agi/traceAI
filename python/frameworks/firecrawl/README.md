# traceAI-firecrawl

OpenTelemetry instrumentation for the Firecrawl v2 client.

## v1 and v2 are different clients

`firecrawl-py` 4.x ships two clients. The v2 client is `from firecrawl import Firecrawl` (and `AsyncFirecrawl`), and it delegates `scrape`, `search`, `map`, and the crawl methods to `firecrawl.v2.client.FirecrawlClient` (and `firecrawl.v2.client_async.AsyncFirecrawlClient`). The v1 client is `FirecrawlApp`, and it is feature-frozen.

This package wraps the v2 client only. It does not wrap `FirecrawlApp`, and it does not import or disable OpenLIT. OpenLIT's `FireCrawlInstrumentor` wraps `FirecrawlApp.scrape_url` on the v1 client. A user who constructs both clients and enables both instrumentors gets two spans. Pick one.

## Supported versions

`firecrawl-py >=4.46.2,<5` on Python 3.10, 3.11 and 3.13. The tests run on `firecrawl-py==4.46.2`. Outside that range `instrument()` logs an error and wraps nothing.

## Usage

```python
from firecrawl import Firecrawl
from fi_instrumentation import register
from traceai_firecrawl import FirecrawlInstrumentor

tracer_provider = register(project_name="firecrawl")
FirecrawlInstrumentor().instrument(tracer_provider=tracer_provider)

# Construct the client after instrument(); see "Client construction order".
client = Firecrawl(api_key="your-firecrawl-api-key")
client.scrape("https://example.com")
```

## What it records

Each call to `scrape`, `search`, `map`, `crawl`, `start_crawl`, `get_crawl_status` or `cancel_crawl`, sync or async, emits one span named `firecrawl.<method>` with `fi.span.kind=TOOL`. A blocking `crawl()` is one span for the whole call: not one per page and not one per status poll. When the async `crawl()` calls `start_crawl()` internally, that inner call adds no span. The span is current while the SDK runs, so HTTP client spans nest under it.

| Attribute | Methods | Value |
|---|---|---|
| `server.address` | `scrape`, `map`, `crawl`, `start_crawl` | Host of the target URL. Never the path or query. |
| `fi.retrieval.query` | `search` | The query, with the API key redacted, capped at 1024 characters. |
| `fi.retrieval.document_count` | `search` | Number of results. |
| `firecrawl.formats` | `scrape`, `search`, `crawl`, `start_crawl` | Names of the requested formats, from `formats=` or `scrape_options.formats`, when the caller passes them. A JSON format's prompt and schema are not recorded. |
| `firecrawl.job_id` | `crawl`, `start_crawl`, `get_crawl_status`, `cancel_crawl` | The crawl job id, positional or keyword. The start, status and cancel spans of one job share it. |
| `firecrawl.limit` | `crawl`, `start_crawl` | The `limit` argument, when passed. |
| `firecrawl.page_count` | `crawl` | Pages the job completed. |
| `firecrawl.status` | `crawl`, `get_crawl_status` | Job status from the response (`scraping`, `completed`, `failed`, `cancelled`). |
| `firecrawl.cancelled` | see "Status" | `true` when the job or the call was cancelled. |
| `firecrawl.credits_used` | any method whose result carries it | Credits the response reports. Not converted to dollars. |
| `firecrawl.error.status_code`, `firecrawl.error.code` | any, on a Firecrawl API error | HTTP status and the vendor's machine-readable error code, when present. |

`firecrawl.credits_used` caveat: for crawl jobs, firecrawl-py fills `credits_used` with `0` when the API response omits `creditsUsed`, so a recorded `0` on `crawl` or `get_crawl_status` can mean either "no credits" or "not reported".

### Status

- The SDK returns a failed or cancelled crawl job without raising. A job with status `failed` sets the span to ERROR `failed`. A job with status `cancelled` sets ERROR `cancelled` and `firecrawl.cancelled=true`.
- `cancel_crawl` sets `firecrawl.cancelled` to the API's answer; the span itself is OK.
- Cancelling an asyncio task that is awaiting a Firecrawl call ends the span ERROR `cancelled` with `firecrawl.cancelled=true`.
- Any exception the SDK raises is recorded on the span, sets ERROR with the exception type and message, and is re-raised unchanged.

## What it never records

This package has no content-capture setting. It never records page content (markdown, HTML, screenshots, links, titles, descriptions), URL paths or query strings, JSON-format prompts or schemas, or the Firecrawl API key. The key is redacted from every attribute the package sets, including a key taken from `FIRECRAWL_API_KEY`. The search query is recorded by design. On an error, the span status and exception event carry the SDK's exception message as raised.

Instrumentation errors never reach your code: if reading an argument or a result fails, the call still returns the SDK's result or raises the SDK's exception, and the span still ends.

## Client construction order

`Firecrawl` and `AsyncFirecrawl` copy the v2 client's methods when they are constructed. Call `instrument()` before you construct them:

- A client constructed before `instrument()` is not traced.
- A client constructed while instrumented keeps tracing after `uninstrument()`. Construct a new client to stop.

`FirecrawlClient` and `AsyncFirecrawlClient` used directly look the methods up on every call, so they follow `instrument()` and `uninstrument()` immediately.

## Watchers

`Firecrawl.watcher()` returns a `Watcher` that polls `get_crawl_status` from its own thread. Each poll is a separate root `firecrawl.get_crawl_status` span with no parent, and a poll error the watcher swallows still shows as an ERROR span. `AsyncFirecrawl.watcher()` polls in the task that iterates it, so its poll spans are children of the span current there.
