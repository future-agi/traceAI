"""Run one traced Exa search against the configured Exa endpoint."""

import os

from exa_py import Exa
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_exa import ExaInstrumentor


def main() -> None:
    tracer_provider = register(
        project_name="exa-example",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    ExaInstrumentor().instrument(tracer_provider=tracer_provider)

    client_options = {"api_key": "exa-dummy-key"}
    base_url = os.getenv("EXA_BASE_URL")
    if base_url:
        client_options["base_url"] = base_url
    client = Exa(**client_options)
    client.search("TraceAI Exa instrumentation example", num_results=1)
    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
