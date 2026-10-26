"""Test probe: export one EverOS span using EverOS's own settings and tracer.

Usage: python everos_config_probe.py "<question>"

Loads ``[observability]`` the way EverOS does (``everos.config.load_settings``:
``EVEROS_OBSERVABILITY__*`` environment variables, then
``$EVEROS_ROOT/everos.toml``, then the shipped defaults), installs EverOS's
tracer with ``init_tracing``, opens one ``everos.memory.search`` span through
EverOS's ``memory_span`` / ``capture_input`` helpers, and flushes with
``shutdown_tracing``. The tests use it to check each way of setting the
endpoint, headers and resource without starting the whole app. Not part of
the recipe. The last stdout line is JSON.
"""

from __future__ import annotations

import json
import sys

from everos.config import load_settings
from everos.core.observability.tracing import (
    capture_input,
    init_tracing,
    memory_span,
    shutdown_tracing,
)


def main() -> None:
    settings = load_settings().observability
    installed = init_tracing(settings)
    with memory_span("everos.memory.search", observation_type="retriever") as span:
        capture_input(span, {"query": sys.argv[1], "top_k": -1, "method": "hybrid"})
    shutdown_tracing()
    print(json.dumps({"installed": installed, "capture_content": settings.capture_content}))


if __name__ == "__main__":
    main()
