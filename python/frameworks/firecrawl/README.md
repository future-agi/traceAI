# traceAI-firecrawl

Trace Firecrawl v2 scraping, extraction, agent and browser operations in Future AGI without exporting page bodies, prompts or browser code.

## Install

```bash
pip install traceAI-firecrawl
```

Supports `firecrawl-py >=4.46.2,<5` on Python 3.10–3.13. Use `Firecrawl` or `AsyncFirecrawl`, not the feature-frozen v1 `FirecrawlApp`. Outside the supported SDK range, `instrument()` logs an error and wraps nothing.

## Configure and run

Set your Future AGI credentials and Firecrawl API key in your environment:

```bash
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
export FIRECRAWL_API_KEY="your-firecrawl-api-key"
```

```python
import os

from firecrawl import Firecrawl
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_firecrawl import FirecrawlInstrumentor

tracer_provider = register(project_name="firecrawl", project_type=ProjectType.OBSERVE)
FirecrawlInstrumentor().instrument(tracer_provider=tracer_provider)

# Construct facade clients after instrumentation.
client = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
client.scrape("https://example.com")
client.batch_scrape(["https://example.com"], formats=["markdown"])

# Flush before a short-lived script exits.
tracer_provider.force_flush()
```

Open the `firecrawl` project in Future AGI's **Observe** view. You will see `firecrawl.scrape` and `firecrawl.batch_scrape` spans with duration, status and available job metadata. Use the same instrumentation setup with `AsyncFirecrawl` and await its operations.

## Supported operations

| API | Methods |
|---|---|
| Scrape, search and map | `scrape`, `search`, `map` |
| Crawl | `crawl`, `start_crawl`, `get_crawl_status`, `cancel_crawl` |
| Extract | `extract`, `start_extract`, `get_extract_status` |
| Batch scrape | `batch_scrape`, `start_batch_scrape`, `get_batch_scrape_status`, `get_batch_scrape_status_page`, `get_batch_scrape_errors`, `cancel_batch_scrape` |
| Agent | `agent`, `start_agent`, `get_agent_status`, `cancel_agent` |
| Browser | `browser`, `browser_execute`, `delete_browser`, `list_browsers` |

These methods are covered on both sync and async clients. The direct `AsyncFirecrawlClient.wait_batch_scrape` method is also covered. Firecrawl marks its extract endpoint as maintenance-only; existing extract calls remain traceable. For new applications, follow [Firecrawl's extractor selection guide](https://docs.firecrawl.dev/developer-guides/usage-guides/choosing-the-data-extractor).

Each user call emits one span named `firecrawl.<method>`. Blocking calls include the whole wait, not a span per page or internal poll. The span is current while the SDK runs, so separately enabled HTTP client instrumentation nests beneath it. The package does not trace remote agent steps, browser activity between API calls, or other SDK APIs outside this table.

## Span metadata

`gen_ai.span.kind` is `RETRIEVER` for `search` and `TOOL` for the other operations.

| Attribute | Value |
|---|---|
| `server.address` | Target URL host for `scrape`, `map`, `crawl` and `start_crawl`. No path or query. URL lists are not exported. |
| `gen_ai.retrieval.query`, `input.value` | Search query, API-key redacted and capped at 1024 characters. Omitted with `hide_inputs` or `hide_input_text`. |
| `firecrawl.result_count` | Search records across SDK `web`, `news`, `images` and `tools` groups. Known empty groups count as zero; unknown shapes omit the attribute. |
| `firecrawl.formats` | Bounded, deduplicated format names for scrape, search, crawl, batch and extract inputs. No prompts or schemas. |
| `firecrawl.job_id` | Job ID supplied to a lifecycle call or returned by the SDK. Start, status and cancel calls can be correlated using this value. |
| `firecrawl.session_id` | Browser session ID returned by `browser` or supplied to `browser_execute` and `delete_browser`. Separate from the application tracing `session.id`. |
| `firecrawl.limit` | Crawl `limit`, when passed. |
| `firecrawl.page_count` | Completed pages for `crawl`, `batch_scrape` and `wait_batch_scrape`. |
| `firecrawl.status` | Recognized job state: `scraping`, `processing`, `completed`, `failed` or `cancelled`. |
| `firecrawl.cancelled` | Cancellation result for cancel APIs, or `true` for a cancelled blocking job or asyncio call. |
| `firecrawl.credits_used` | Credits reported by the SDK, including `credits_billed` on browser deletion. Not converted to dollars. |
| `firecrawl.error.status_code`, `firecrawl.error.code` | HTTP status and recognized machine-readable API error code, when present. |

Job IDs are omitted when neither the caller nor the SDK result provides one. In particular, the SDK's blocking batch result and pagination-page result do not contain an ID. To correlate separate batch operations, use `start_batch_scrape` followed by status and cancel calls with its ID.

Credits are recorded only when available on the SDK result. The SDK defaults some crawl/batch credit fields to zero, so zero may mean unreported usage. Its async extract implementation can omit the API's `creditsUsed` field; this integration does not infer missing credits.

Format metadata follows SDK serialization: `scrape_options` takes precedence over convenience `formats`. A `ScrapeFormats` container contributes its explicit list and serialized enabled flags, including its default `markdown=true`. The SDK does not serialize its `images` or `json` boolean flags; explicit format entries still contribute metadata. Search counts include tool discovery records, not pages fetched by those tools.

### Status and errors

- Blocking crawl, batch, extract and agent calls that return a failed or cancelled job end with ERROR. A successful status poll stays OK and records the job state; a rejected request is an error.
- Cancel APIs record their Boolean result in `firecrawl.cancelled`; the API call itself stays OK.
- Extract, agent and browser responses with `success=false` end with ERROR `failed`. A browser execution with a nonzero exit code ends with ERROR `execution_failed`.
- Cancelling an asyncio task ends its span with ERROR `cancelled` and `firecrawl.cancelled=true`.
- SDK exceptions add one safe exception event and end with ERROR. Exception text and tracebacks are omitted; your code receives the original exception unchanged.

## Privacy

This package has no content-capture setting. It does not export page bodies, screenshots, titles, descriptions, extracted data, agent prompts or results, schemas, browser code or output, CDP/live-view URLs, request URL paths or query strings, or the Firecrawl API key. Search query text is the deliberate exception. The API key is redacted from free-text metadata, including keys resolved from `FIRECRAWL_API_KEY`.

Pass `config=TraceConfig(...)` from `fi_instrumentation.instrumentation` to `instrument()` for shared privacy controls:

- `hide_inputs` or `hide_input_text` omits search query text.
- `pii_redaction` (or `FI_PII_REDACTION=true`) redacts PII in query text before truncation.
- `None` uses the shared `FI_*` environment settings. Other config types raise `TypeError`.

Message, image and output hide flags do not add content capture. Shared `suppress_tracing()` suppresses Firecrawl spans; `using_session()` supplies the application session attribute. If API-key discovery fails, free-text metadata is omitted. Instrumentation metadata errors do not replace the SDK's result or exception.

## Client construction order

`Firecrawl` and `AsyncFirecrawl` copy the v2 client's bound methods during construction. Call `instrument()` first:

- A facade client constructed before instrumentation is not traced; construct a new one afterward.
- A facade client constructed while instrumented stops tracing at `uninstrument()`.

Direct `FirecrawlClient` and `AsyncFirecrawlClient` instances look methods up on each call and follow instrumentation changes immediately. This package does not instrument v1 `FirecrawlApp` or enable or disable other vendors' instrumentors.

## Watchers

`Firecrawl.watcher()` polls `get_crawl_status` from its own thread. Each poll produces a separate root span; a swallowed poll error still appears as ERROR. `AsyncFirecrawl.watcher()` polls in the iterating task, so its spans inherit that task's current parent.
