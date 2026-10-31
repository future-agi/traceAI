"""FI instrumentation for AgentScope's agent, tool, model and pipeline APIs."""

from ._instrumentor import AgentScopeInstrumentor
from .version import __version__

__all__ = ["AgentScopeInstrumentor", "__version__"]
