"""``opentelemetry-instrument`` entry point: starts the Future AGI agent next to an existing OTel setup.

Registered as the ``futureagi`` instrumentor. It starts the agent only when ``FI_AUTO_INSTRUMENT`` is
true, so ``FI_AUTO_INSTRUMENT=true opentelemetry-instrument python app.py`` exports to Future AGI.
Future AGI keys or a ``futureagi.toml`` alone never start it: they may belong to a manual setup in
the same app. ``opentelemetry-instrument`` loads every installed entry point at each start, so
loading this one imports nothing from the agent. Disable it with
``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=futureagi``.
"""

import os
from typing import Any, Collection

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

_TRUE = ("1", "true", "yes", "on")


class FutureAGIInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    def instrumentation_dependencies(self) -> Collection[str]:
        return ()

    def _instrument(self, **kwargs: Any) -> None:
        if os.environ.get("FI_AUTO_INSTRUMENT", "").strip().lower() not in _TRUE:
            return
        from fi_instrumentation.auto._bootstrap import start

        start()

    def _uninstrument(self, **kwargs: Any) -> None:
        from fi_instrumentation.auto import _bootstrap

        if not _bootstrap._started:
            return
        from fi_instrumentation.auto._manager import shutdown

        shutdown()
