"""Zero-code tracing for Future AGI.

Without code changes::

    fi-instrument python app.py                                 # launcher
    FI_AUTO_INSTRUMENT=true python app.py                       # environment only
    FI_AUTO_INSTRUMENT=true opentelemetry-instrument python app.py   # next to an OpenTelemetry setup

With one line::

    import fi_instrumentation
    fi_instrumentation.init()

Every installed traceAI integration is enabled when its library is first imported. Libraries
without an installed traceAI package are not traced; ``fi-instrument doctor`` lists them.
"""

from importlib import import_module
from typing import Any

# Loaded on first use: the opentelemetry-instrument entry point and the start hooks live in this
# package, and must not load the agent unless it starts.
_EXPORTS = {
    "init": "_manager",
    "auto_instrument": "_manager",
    "plan": "_manager",
    "report": "_manager",
    "format_report": "_manager",
    "flush": "_manager",
    "shutdown": "_manager",
    "IntegrationStatus": "_manager",
    "INSTRUMENTED": "_manager",
    "PENDING": "_manager",
    "NOT_INSTALLED": "_manager",
    "MISSING_INSTRUMENTOR": "_manager",
    "DISABLED": "_manager",
    "STATUS_MANUAL": "_manager",
    "FAILED": "_manager",
    "resolve": "_config",
    "AutoConfig": "_config",
    "ConfigError": "_config",
    "CONFIG_TEMPLATE": "_config",
}

__all__ = [
    "init",
    "auto_instrument",
    "plan",
    "report",
    "format_report",
    "flush",
    "shutdown",
    "resolve",
    "AutoConfig",
    "ConfigError",
    "IntegrationStatus",
    "CONFIG_TEMPLATE",
    "INSTRUMENTED",
    "PENDING",
    "NOT_INSTALLED",
    "MISSING_INSTRUMENTOR",
    "DISABLED",
    "STATUS_MANUAL",
    "FAILED",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{module}"), name)
