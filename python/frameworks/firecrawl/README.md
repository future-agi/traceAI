# traceAI-firecrawl

OpenTelemetry instrumentation for the Firecrawl v2 client.

## v1 and v2 are different clients

`firecrawl-py` 4.46.2 ships two clients. The v2 client is `from firecrawl import Firecrawl`, and it delegates `scrape`, `search`, `map`, and `crawl` to `firecrawl.v2.client.FirecrawlClient`. The v1 client is `FirecrawlApp`, and it is feature-frozen.

This package wraps the v2 client only. It does not wrap `FirecrawlApp`, and it does not import or disable OpenLIT. OpenLIT's `FireCrawlInstrumentor` wraps `FirecrawlApp.scrape_url` on the v1 client. A user who constructs both clients and enables both instrumentors gets two spans. Pick one.

## What it records

Each span records `fi.span.kind=TOOL`. `search` also records the query, capped at 1024 characters, and the result count. `scrape` and `map` record the URL host only, never the path or the body. `crawl` emits one span for the whole call, not one per page, and records the job id, the limit, and the page count.

Content capture is off by default. The API key is never copied into an attribute. `credits_used` is recorded only when the response carries it, and it is not converted to dollars.

## Usage

```python
from firecrawl import Firecrawl
from fi_instrumentation import register
from traceai_firecrawl import FirecrawlInstrumentor

tracer_provider = register(project_name="firecrawl")
FirecrawlInstrumentor().instrument(tracer_provider=tracer_provider)

client = Firecrawl(api_key="your-firecrawl-api-key")
client.scrape("https://example.com")
```
