# traceAI-firecrawl

OpenTelemetry instrumentation for the Firecrawl v2 client.

## v1 and v2 are different clients

`firecrawl-py` 4.x ships two clients. The v2 client is `from firecrawl import Firecrawl` (and `AsyncFirecrawl`), and it delegates `scrape`, `search`, `map`, and the crawl methods to `firecrawl.v2.client.FirecrawlClient` (and `firecrawl.v2.client_async.AsyncFirecrawlClient`). The v1 client is `FirecrawlApp`, and it is feature-frozen.

This package wraps the v2 client only. It does not wrap `FirecrawlApp`, and it does not import or disable OpenLIT. OpenLIT's `FireCrawlInstrumentor` wraps `FirecrawlApp.scrape_url` on the v1 client. A user who constructs both clients and enables both instrumentors gets two spans. Pick one.

## Supported versions

`firecrawl-py >=4.46.2,<5` on Python 3.10, 3.11, 3.12 and 3.13. The tests run on `firecrawl-py==4.46.2`. Outside that range `instrument()` logs an error and wraps nothing.

## Usage

```python
from firecrawl import Firecrawl
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_firecrawl import FirecrawlInstrumentor

tracer_provider = register(project_name="firecrawl", project_type=ProjectType.OBSERVE)
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
| `fi.retrieval.query` | `search` | The query, with the API key redacted, capped at 1024 characters. Omitted with `hide_inputs` or `hide_input_text`; shared PII redaction applies when enabled. |
| `fi.retrieval.document_count` | `search` | Total returned records across SDK `web`, `news`, `images` and `tools` groups. Known empty groups count as zero; unknown or malformed shapes omit the attribute. |
| `firecrawl.formats` | `scrape`, `search`, `crawl`, `start_crawl` | Bounded, deduplicated canonical SDK format names from `formats=` or `scrape_options.formats`. Unknown names, prompts and schemas are omitted. Snake-case aliases use the SDK's API spelling. |
| `firecrawl.job_id` | `crawl`, `start_crawl`, `get_crawl_status`, `cancel_crawl` | The crawl job id, positional or keyword. The start, status and cancel spans of one job share it. |
| `firecrawl.limit` | `crawl`, `start_crawl` | The `limit` argument, when passed. |
| `firecrawl.page_count` | `crawl` | Pages the job completed. |
| `firecrawl.status` | `crawl`, `get_crawl_status` | Job status from the response (`scraping`, `completed`, `failed`, `cancelled`). |
| `firecrawl.cancelled` | see "Status" | `true` when the job or the call was cancelled. |
| `firecrawl.credits_used` | any method whose result carries it | Credits the response reports. Not converted to dollars. |
| `firecrawl.error.status_code`, `firecrawl.error.code` | any, on a Firecrawl API error | HTTP status and the vendor's machine-readable error code, when present. |

`firecrawl.credits_used` caveat: for crawl jobs, firecrawl-py fills `credits_used` with `0` when the API response omits `creditsUsed`, so a recorded `0` on `crawl` or `get_crawl_status` can mean either "no credits" or "not reported".

Format metadata follows SDK 4.46.2's payload: `scrape_options` takes precedence over convenience `formats`. A `ScrapeFormats` container contributes its explicit list and serialized enabled flags, including its default `markdown=true`. The SDK does not serialize its `images` or `json` boolean flags, so those flags add no metadata; explicit format entries still do.

Search counts include `tools` because SDK 4.46.2 returns a list of `DiscoveredTool` discovery records, alongside web/news/image records. The count includes those records, not pages fetched by a tool. The prior generic `data`/`results`/`web` list fallback remains supported; no result contents are exported.

### Status

- The SDK returns a failed or cancelled crawl job without raising. On a blocking `crawl()`, a job with status `failed` sets the span to ERROR `failed`, and a job with status `cancelled` sets ERROR `cancelled` and `firecrawl.cancelled=true`. A `get_crawl_status` poll that gets an answer stays OK and records the job's state in `firecrawl.status`.
- `cancel_crawl` sets `firecrawl.cancelled` to the API's answer; the span itself is OK.
- Cancelling an asyncio task that is awaiting a Firecrawl call ends the span ERROR `cancelled` with `firecrawl.cancelled=true`.
- Any exception the SDK raises adds one safe exception event and sets ERROR with a recognized exception type (otherwise `Exception`). Exception free text and serialized tracebacks are omitted; the original exception is re-raised unchanged.

## What it never records

This package has no content-capture setting. It never records page content (markdown, HTML, screenshots, links, titles, descriptions), URL paths or query strings, JSON-format prompts or schemas, or the Firecrawl API key. The key is redacted from every attribute the package sets, including a key taken from `FIRECRAWL_API_KEY`. The search query is recorded by design. Error events contain a safe type and generic message; status descriptions contain that type or `cancelled`. Only HTTP status and recognized machine codes are retained from API errors; unknown codes are omitted.

Instrumentation errors never reach your code: if reading an argument or a result fails, the call still returns the SDK's result or raises the SDK's exception, and the span still ends.

Pass `config=TraceConfig(...)` from `fi_instrumentation.instrumentation` to `instrument()` to use shared privacy controls. `None` creates `TraceConfig()` using its `FI_*` environment settings; other objects raise `TypeError`. `hide_inputs` or `hide_input_text` omits the search query. `pii_redaction` (or `FI_PII_REDACTION=true`) applies the shared regex redactor to query text before truncation, followed by FITracer attribute masking. Other message, image and output hide flags are not applicable because no messages or page bodies are exported. Shared `suppress_tracing()` emits no Firecrawl spans, and `using_session()` supplies the shared session attribute. If API-key discovery fails, free-text attributes are omitted while safe format/count metadata remains. Error events and status use the separate safe metadata policy described above.

## Client construction order

`Firecrawl` and `AsyncFirecrawl` copy the v2 client's methods when they are constructed. Call `instrument()` before you construct them:

- A client constructed before `instrument()` is not traced.
- A client constructed while instrumented keeps tracing after `uninstrument()`. Construct a new client to stop.

`FirecrawlClient` and `AsyncFirecrawlClient` used directly look the methods up on every call, so they follow `instrument()` and `uninstrument()` immediately.

## Watchers

`Firecrawl.watcher()` returns a `Watcher` that polls `get_crawl_status` from its own thread. Each poll is a separate root `firecrawl.get_crawl_status` span with no parent, and a poll error the watcher swallows still shows as an ERROR span. `AsyncFirecrawl.watcher()` polls in the task that iterates it, so its poll spans are children of the span current there.
