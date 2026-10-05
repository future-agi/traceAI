"""Scenario script: one ``cognee.add`` call, with tracing set up per argv[1].

    python cognee_add.py otlp-env [URL ...]  # no register(); Cognee's own OTLP exporter
    python cognee_add.py no-readd            # register() without the recipe's re-add line

Prints one JSON line with the number of spans Cognee's in-memory buffer holds,
so the test can tell "Cognee made no spans" apart from "spans were made but
not exported". In otlp-env mode it also reports, for each URL argument,
whether Cognee 1.6.2 treats it as HTTP-only (otherwise Cognee picks the gRPC
exporter when that package is installed).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

DOCUMENT = "Ada works on the Lighthouse project."


def main(argv: list[str]) -> int:
    mode = argv[1]
    provider = None
    if mode == "no-readd":
        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType

        provider = register(
            project_name=os.environ["FI_PROJECT_NAME"],
            project_type=ProjectType.OBSERVE,
            set_global_tracer_provider=True,
            verbose=False,
        )
    elif mode != "otlp-env":
        raise SystemExit("unknown mode: " + mode)

    import cognee

    asyncio.run(cognee.add(DOCUMENT))
    report = {
        "mode": mode,
        "buffered_spans": sum(len(trace.spans()) for trace in cognee.get_all_traces()),
    }
    if mode == "otlp-env":
        from cognee.modules.observability.tracing import _requires_http_exporter

        report["http_only"] = {url: _requires_http_exporter(url) for url in argv[2:]}
    print(json.dumps(report))
    if provider is not None:
        provider.force_flush()
    # In otlp-env mode Cognee's own provider flushes its BatchSpanProcessor
    # at interpreter exit.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
