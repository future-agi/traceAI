"""Process-start entry used by ``fi-instrument``, ``FI_AUTO_INSTRUMENT`` and ``opentelemetry-instrument``.

Fails open: whatever goes wrong, the application starts as if the agent were not there.
"""

import logging

logger = logging.getLogger("fi_instrumentation.auto")

_started = False


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    try:
        if not logger.handlers and not logging.getLogger().handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            logger.propagate = False
            logger.setLevel(logging.WARNING)
        from fi_instrumentation.auto._manager import init

        init()
    except Exception as error:
        logger.warning("traceAI: auto-instrumentation is off: %s", error)
