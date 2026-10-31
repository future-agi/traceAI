"""Export AgentScope's native OpenTelemetry spans to Future AGI."""

from ._lifecycle import Handle, configure
from ._middleware import TracingMiddleware

__version__ = "0.1.0"  # x-release-please-version
__all__ = ["configure", "Handle", "TracingMiddleware", "__version__"]
