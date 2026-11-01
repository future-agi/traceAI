"""Trace one Parallel search and one extract.

parallel-web reads PARALLEL_API_KEY and, when set, PARALLEL_BASE_URL itself.
register() reads FI_API_KEY, FI_SECRET_KEY and FI_BASE_URL.
"""

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from parallel import Parallel

from traceai_parallel import ParallelInstrumentor


def main() -> None:
    tracer_provider = register(
        project_name="parallel-example",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    ParallelInstrumentor().instrument(tracer_provider=tracer_provider)

    client = Parallel()
    search = client.search(search_queries=["traceAI Parallel example"], mode="turbo")
    client.extract(urls=[search.results[0].url])
    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
