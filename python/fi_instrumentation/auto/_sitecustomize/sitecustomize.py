"""Loaded at interpreter start by ``fi-instrument``: starts the agent, then the app's own sitecustomize."""

import importlib.machinery
import importlib.util
import os
import sys

from fi_instrumentation.auto._bootstrap import start

start()

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.machinery.PathFinder.find_spec(
    "sitecustomize", [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]
)
if _spec is not None and _spec.loader is not None and _spec.origin and os.path.dirname(os.path.abspath(_spec.origin)) != _here:
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["sitecustomize"] = _module
    _spec.loader.exec_module(_module)
