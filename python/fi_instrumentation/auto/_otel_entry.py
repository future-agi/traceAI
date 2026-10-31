"""``opentelemetry-instrument`` entry point: loads the Future AGI agent next to an existing OTel setup.

Registered as the ``futureagi`` instrumentor, so ``opentelemetry-instrument python app.py``
also exports to Future AGI when FI_API_KEY/FI_SECRET_KEY are set. Disable it with
``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=futureagi``.
"""

from typing import Any, Collection

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor


class FutureAGIInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    def instrumentation_dependencies(self) -> Collection[str]:
        return ()

    def _instrument(self, **kwargs: Any) -> None:
        import logging
        import os

        from fi_instrumentation.auto._bootstrap import start
        from fi_instrumentation.auto._config import CONFIG_FILE_ENV, DEFAULT_CONFIG_FILE

        # Installed is not opted in: without any Future AGI setting, stay quiet.
        configured = (os.environ.get("FI_API_KEY") or os.environ.get("FI_SECRET_KEY")
                      or os.environ.get(CONFIG_FILE_ENV) or os.path.isfile(DEFAULT_CONFIG_FILE))
        if not configured:
            logging.getLogger("fi_instrumentation.auto").info("traceAI: no Future AGI settings found; not exporting")
            return
        start()

    def _uninstrument(self, **kwargs: Any) -> None:
        from fi_instrumentation.auto._manager import shutdown

        shutdown()
