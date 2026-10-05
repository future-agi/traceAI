"""Trace one Tavily search with traceAI-tavily.

Needs TAVILY_API_KEY plus the Future AGI settings register() reads
(FI_API_KEY, FI_SECRET_KEY, and FI_BASE_URL for a self-hosted collector).
TAVILY_BASE_URL is optional and only points the client at another Tavily
endpoint, such as a local test server.
"""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from tavily import TavilyClient

from traceai_tavily import TavilyInstrumentor


def main() -> None:
    tracer_provider = register(
        project_name="tavily-example",
        project_type=ProjectType.OBSERVE,
        verbose=False,
    )
    TavilyInstrumentor().instrument(tracer_provider=tracer_provider)

    client = TavilyClient(
        api_key=os.environ["TAVILY_API_KEY"],
        api_base_url=os.environ.get("TAVILY_BASE_URL") or None,
    )
    response = client.search("What is OpenTelemetry?", max_results=2)
    print("results: {0}".format(len(response["results"])))
    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
