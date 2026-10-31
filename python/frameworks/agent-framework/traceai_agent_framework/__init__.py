"""
TraceAI Microsoft Agent Framework integration.

Future AGI integration for Microsoft Agent Framework. We add a SpanProcessor
to the user's TracerProvider that re-keys the framework's native ``gen_ai.*``
attributes into Future AGI conventions on every span as it ends.
"""

from .integration import enable_fi_attribute_mapping
from .processor import AgentFrameworkSpanProcessor
from .version import __version__

__all__ = [
    "enable_fi_attribute_mapping",
    "AgentFrameworkSpanProcessor",
    "__version__",
]
