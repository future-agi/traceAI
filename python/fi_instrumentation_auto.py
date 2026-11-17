"""Starts Future AGI tracing when an interpreter starts with FI_AUTO_INSTRUMENT set.

``fi_instrumentation_auto.pth`` imports this module only when ``FI_AUTO_INSTRUMENT`` is true. It
never stops the interpreter from starting: when ``fi_instrumentation`` cannot be imported it prints
one line, once for the whole process tree, and the program runs untraced.
"""

import os
import sys

# Set once the line is printed; child processes inherit it and stay quiet.
WARNED = "FI_AUTO_INSTRUMENT_WARNED"

# opentelemetry-instrument and the OpenTelemetry Operator put the directory of their sitecustomize
# on PYTHONPATH. That sitecustomize configures the application's tracer provider and then loads the
# futureagi entry point, which starts the agent on that provider. Starting here, earlier, would
# make the agent's own provider the global one first.
_OPENTELEMETRY_START = os.path.join("opentelemetry", "instrumentation", "auto_instrumentation")


def _under_opentelemetry_instrument() -> bool:
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    return any(entry and os.path.normpath(entry).endswith(_OPENTELEMETRY_START) for entry in entries)


def warn_once(error: BaseException) -> None:
    if os.environ.get(WARNED):
        return
    os.environ[WARNED] = "1"
    sys.stderr.write(f"traceAI: tracing is off: {sys.executable} cannot import fi_instrumentation "
                     f"({type(error).__name__}). Install fi-instrumentation-otel in this environment.\n")


def start() -> None:
    if _under_opentelemetry_instrument():
        return
    try:
        from fi_instrumentation.auto._bootstrap import start as start_agent
    except Exception as error:
        warn_once(error)
        return
    start_agent()
