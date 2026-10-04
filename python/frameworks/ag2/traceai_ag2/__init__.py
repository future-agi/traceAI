"""Future AGI tracing for AG2 1.x (PyPI ``ag2``, import ``ag2``).

AG2 1.x already emits OpenTelemetry GenAI spans from its own
``TelemetryMiddleware``. This package does not wrap ``Agent``. It:

* attaches ``TelemetryMiddleware`` with ``capture_content=False``
  (``setup`` / ``create_telemetry_middleware``),
* registers the Future AGI exporter through ``fi_instrumentation.register()``
  when you do not pass a provider,
* installs :class:`AG2SpanProcessor`, which sets ``gen_ai.span.kind`` and
  aliases AG2's usage keys onto the GenAI semconv names.

This is not Microsoft AutoGen (``autogen-agentchat``, see ``traceai-autogen``)
and not AG2 Classic (``autogen`` 0.14.x). It never imports ``autogen``.
"""

from ._processor import (
    AG2_INSTRUMENTATION_SCOPE,
    OPERATION_TO_SPAN_KIND,
    USAGE_KEY_ALIASES,
    AG2SpanProcessor,
    normalize_attributes,
    span_kind_for,
)
from ._setup import create_telemetry_middleware, install_span_processor, setup
from .version import __version__

__all__ = [
    "AG2SpanProcessor",
    "AG2_INSTRUMENTATION_SCOPE",
    "OPERATION_TO_SPAN_KIND",
    "USAGE_KEY_ALIASES",
    "create_telemetry_middleware",
    "install_span_processor",
    "normalize_attributes",
    "setup",
    "span_kind_for",
    "__version__",
]
