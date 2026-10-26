"""Future AGI tracing for AG2 Classic (the ``autogen`` distribution, 0.14.x).

Native-first: AG2 Classic ships ``autogen.opentelemetry``. ``setup()`` calls
its ``instrument_llm_wrapper`` / ``instrument_agent`` / ``instrument_pattern``
on the Future AGI tracer provider you pass, and adds a span processor that maps
the upstream keys to Future AGI conventions. Importing this package does not
import ``autogen``; ``setup()`` does, after the version guard.
"""

from ._guard import AG2ClassicCompatibilityError, check_autogen_classic
from ._processor import (
    AG2_SCOPE,
    CONTENT_KEYS,
    KIND_BY_SPAN_TYPE,
    AG2ClassicSpanProcessor,
    kind_for_span_type,
    map_ag2_attributes,
)
from ._setup import AG2ClassicTracing, setup
from .version import __version__

__all__ = [
    "AG2_SCOPE",
    "CONTENT_KEYS",
    "KIND_BY_SPAN_TYPE",
    "AG2ClassicCompatibilityError",
    "AG2ClassicSpanProcessor",
    "AG2ClassicTracing",
    "check_autogen_classic",
    "kind_for_span_type",
    "map_ag2_attributes",
    "setup",
    "__version__",
]
