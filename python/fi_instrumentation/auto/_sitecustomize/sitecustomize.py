"""Loaded at interpreter start by ``fi-instrument``: starts the agent, then the app's own sitecustomize."""

import importlib.machinery
import importlib.util
import os
import sys

try:
    from fi_instrumentation.auto._bootstrap import start

    start()
except Exception as error:  # never stop the application from starting
    # fi-instrument may come from another environment (pipx, another venv) than the Python it
    # runs. Say so once for the whole process tree: children inherit the marker. The .pth start
    # uses the same marker, so the two never both print.
    if (os.environ.get("FI_AUTO_INSTRUMENT", "").strip().lower() in ("1", "true", "yes", "on")
            and not os.environ.get("FI_AUTO_INSTRUMENT_WARNED")):
        os.environ["FI_AUTO_INSTRUMENT_WARNED"] = "1"
        sys.stderr.write(f"traceAI: tracing is off: {sys.executable} cannot import fi_instrumentation "
                         f"({type(error).__name__}). Install fi-instrumentation-otel in this environment.\n")

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.machinery.PathFinder.find_spec(
    "sitecustomize", [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]
)
if _spec is not None and _spec.loader is not None and _spec.origin and os.path.dirname(os.path.abspath(_spec.origin)) != _here:
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["sitecustomize"] = _module
    _spec.loader.exec_module(_module)
