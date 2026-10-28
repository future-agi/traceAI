"""TH-8327 measurement: what does a bare Tavily client export with register()?

Run by tests/test_measurement.py through ``harness.run``. The script calls the
real tavily-python client (``TavilyClient`` and ``AsyncTavilyClient``,
``search`` and ``extract``) against the loopback fake in ``TAVILY_BASE_URL``,
with only ``fi_instrumentation.register()`` exporting to ``FI_BASE_URL``.

All calls run inside one ``measurement.control`` span from the registered
provider. That span proves the export path works, so any other span that
reaches the receiver came from Tavily instrumentation.

Options:
  --with-langchain-instrumentor  also instrument traceAI-langchain, the existing
                                 traceAI instrumentor that traces Tavily as a
                                 LangChain tool.
  --with-tavily-instrumentor     also instrument traceAI-tavily (if installed).

Prints one JSON line with what the client returned (counts only).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from tavily import AsyncTavilyClient, TavilyClient

QUERY = "th-8327 bare client measurement"
URLS = ["https://example.com/a", "https://example.com/b"]


async def _async_calls(api_key: str, base_url: str) -> dict:
    client = AsyncTavilyClient(api_key=api_key, api_base_url=base_url)
    try:
        search = await client.search(QUERY, max_results=2)
        extract = await client.extract(URLS)
    finally:
        await client.close()
    return {
        "async_search_results": len(search["results"]),
        "async_extract_results": len(extract["results"]),
    }


def main() -> None:
    # Global as well: a library with native OpenTelemetry support would pick up
    # the global provider, so this is the most generous setup for the vendor.
    provider = register(
        project_name=os.environ.get("MEASURE_PROJECT", "tavily-measurement"),
        project_type=ProjectType.OBSERVE,
        batch=False,
        set_global_tracer_provider=True,
        verbose=False,
    )
    instrumented = []
    if "--with-langchain-instrumentor" in sys.argv:
        from traceai_langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument(tracer_provider=provider)
        instrumented.append("traceai_langchain")
    if "--with-tavily-instrumentor" in sys.argv:
        from traceai_tavily import TavilyInstrumentor

        TavilyInstrumentor().instrument(tracer_provider=provider)
        instrumented.append("traceai_tavily")

    api_key = os.environ["TAVILY_API_KEY"]
    base_url = os.environ["TAVILY_BASE_URL"]
    tracer = provider.get_tracer("th8327.measurement")
    with tracer.start_as_current_span("measurement.control"):
        client = TavilyClient(api_key=api_key, api_base_url=base_url)
        search = client.search(QUERY, max_results=2)
        extract = client.extract(URLS)
        client.close()
        summary = {
            "instrumented": instrumented,
            "search_results": len(search["results"]),
            "extract_results": len(extract["results"]),
        }
        summary.update(asyncio.run(_async_calls(api_key, base_url)))
    provider.force_flush()
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
