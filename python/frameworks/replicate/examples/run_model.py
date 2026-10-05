"""Trace one Replicate prediction with the module-level ``replicate.run``.

The official client reads REPLICATE_API_TOKEN (and REPLICATE_BASE_URL, if
set) from the environment; this script never passes the token anywhere.
"""

import os

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

MODEL = os.getenv("REPLICATE_MODEL", "meta/meta-llama-3-8b-instruct")


def main() -> None:
    tracer_provider = register(
        project_name="replicate-example",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    ReplicateInstrumentor().instrument(tracer_provider=tracer_provider)

    output = replicate.run(MODEL, input={"prompt": "What is the capital of France?"})
    print("".join(str(part) for part in output) if isinstance(output, list) else output)
    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
