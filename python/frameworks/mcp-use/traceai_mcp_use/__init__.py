"""Trace mcp-use agents with Future AGI.

``FutureAGICallback`` is a LangChain callback handler. Pass it to the agent:
``MCPAgent(llm=..., client=..., callbacks=[FutureAGICallback(tracer_provider=...)])``.
It records one span per agent run, LLM call and tool call. It patches
nothing, imports neither ``langfuse`` nor ``traceai_mcp``, and sets no
environment variables.
"""

from traceai_mcp_use._callback import AGENT_SPAN_NAME, FutureAGICallback
from traceai_mcp_use.version import __version__

__all__ = ["AGENT_SPAN_NAME", "FutureAGICallback", "__version__"]
