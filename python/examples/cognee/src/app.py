"""Send Cognee's own OpenTelemetry spans to Future AGI.

Recipe example, not a package. Cognee 1.6.2 creates its spans itself when
``COGNEE_TRACING_ENABLED=true``. This file registers a Future AGI tracer
provider as the global OpenTelemetry provider before Cognee's first traced
call, so Cognee attaches to that provider instead of creating its own
(``setup_tracing`` in ``cognee/modules/observability/tracing.py``). It adds
no instrumentor and never calls ``setup_tracing``. The Future AGI exporter is
wrapped in ``CogneeExportFilter`` (``cognee_filter.py``, next to this file),
which keeps Cognee's prompts, documents, answers and queries out of the
export and marks search and LLM spans.

    COGNEE_TRACING_ENABLED=true python src/app.py "Who works on Lighthouse?"

FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME and (optionally) FI_BASE_URL are
read from the environment; so are Cognee's own LLM_* and EMBEDDING_*
settings, and COGNEE_FI_CAPTURE_CONTENT=true to export content after all.
See README.md.
"""

from __future__ import annotations

import asyncio
import os
import sys

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import BatchSpanProcessor, HTTPSpanExporter

from cognee_filter import CogneeExportFilter

DOCUMENT = "Ada works on the Lighthouse project."


def init_tracing():
    """Register the Future AGI provider. Call once, before any Cognee call."""
    provider = register(
        project_name=os.environ["FI_PROJECT_NAME"],
        project_type=ProjectType.OBSERVE,
        # Cognee reuses the global provider when one is set.
        set_global_tracer_provider=True,
        verbose=False,
    )
    # register() marks its exporter as a default that the next
    # add_span_processor() call replaces (TH-8394). On its first traced call
    # Cognee calls add_span_processor() on the global provider to attach its
    # in-memory buffer, which would silently drop the Future AGI exporter.
    # Adding the exporter again makes it a regular processor that Cognee's
    # call leaves in place. HTTPSpanExporter() reads FI_BASE_URL, FI_API_KEY
    # and FI_SECRET_KEY like register()'s own exporter; if you pass exporter
    # or batch options to register(), pass the same ones here.
    exporter = CogneeExportFilter(HTTPSpanExporter())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


async def remember_and_ask(question: str, document: str) -> list:
    import cognee

    await cognee.add(document)
    await cognee.cognify()
    return await cognee.search(question, query_type=cognee.SearchType.GRAPH_COMPLETION)


def main(argv: list[str]) -> int:
    provider = init_tracing()
    question = argv[1] if len(argv) > 1 else "Who works on Lighthouse?"
    document = argv[2] if len(argv) > 2 else DOCUMENT
    # Cognee starts a new root span for each API call (and `add` ends its
    # memory.store span before ingestion runs), so one parent span here is
    # what puts add, cognify and search in one trace.
    with provider.get_tracer(__name__).start_as_current_span("remember_and_ask"):
        results = asyncio.run(remember_and_ask(question, document))
    for result in results:
        print(result)
    provider.force_flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
