"""Zero-code tracing for Future AGI.

Without code changes::

    fi-instrument python app.py              # launcher
    FI_AUTO_INSTRUMENT=true python app.py    # environment only
    opentelemetry-instrument python app.py   # alongside an existing OpenTelemetry setup

With one line::

    import fi_instrumentation
    fi_instrumentation.init()

Every installed traceAI integration is enabled when its library is first imported. Libraries
without an installed traceAI package are not traced; ``fi-instrument doctor`` lists them.
"""

from fi_instrumentation.auto._config import CONFIG_TEMPLATE, AutoConfig, ConfigError, resolve
from fi_instrumentation.auto._manager import (
    DISABLED,
    FAILED,
    INSTRUMENTED,
    MISSING_INSTRUMENTOR,
    NOT_INSTALLED,
    PENDING,
    STATUS_MANUAL,
    IntegrationStatus,
    auto_instrument,
    flush,
    format_report,
    init,
    plan,
    report,
    shutdown,
)

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
