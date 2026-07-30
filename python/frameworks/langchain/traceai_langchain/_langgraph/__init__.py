"""LangGraph instrumentation module for TraceAI.

LangGraph tracing is provided automatically by ``LangChainInstrumentor``'s callback
handler (node/tool/LLM spans, graph-node enrichment, session grouping, HITL-interrupt
handling). ``LangGraphInstrumentor`` is retained as a deprecated no-op shim.
"""

from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor
from traceai_langchain._langgraph._attributes import LangGraphAttributes, LangGraphSpanKind

__all__ = [
    "LangGraphInstrumentor",
    "LangGraphAttributes",
    "LangGraphSpanKind",
]
