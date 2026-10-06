"""Run one traced Firecrawl scrape against the configured endpoint."""

import os

from firecrawl import Firecrawl
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_firecrawl import FirecrawlInstrumentor


def main() -> None:
    tracer_provider = register(
        project_name="firecrawl-example",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    FirecrawlInstrumentor().instrument(tracer_provider=tracer_provider)

    # firecrawl-py 4.46.2 takes api_url, confirmed from Firecrawl.__init__.
    client = Firecrawl(
        api_key="firecrawl-dummy-key",
        api_url=os.getenv("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev"),
    )
    client.scrape("https://example.com")
    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
