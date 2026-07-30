"""LangGraph instrumentor (deprecated no-op shim).

LangGraph node/tool/LLM spans, graph-node enrichment (``gen_ai.agent.graph.*``),
session grouping, and HITL-interrupt handling are captured automatically by
``LangChainInstrumentor``'s callback handler. This class no longer patches LangGraph
(the previous approach wrapped ``StateGraph.add_node``/``compile`` and broke async
nodes, corrupted concurrent state, and traced interrupts as errors). It remains
importable, and ``instrument()`` is a safe no-op, purely for backward compatibility.
"""

import logging
from typing import Any, Collection, Optional

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("langgraph >= 0.2.0",)


class LangGraphInstrumentor(BaseInstrumentor):
    """Deprecated no-op instrumentor for LangGraph.

    Tracing is provided automatically by ``LangChainInstrumentor``; you can remove the
    ``LangGraphInstrumentor().instrument()`` call. ``instrument()`` only logs a
    one-time deprecation notice.
    """

    _instance: Optional["LangGraphInstrumentor"] = None
    _is_instrumented: bool = False
    _deprecation_logged: bool = False

    def __new__(cls) -> "LangGraphInstrumentor":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        self._is_instrumented = True
        if not LangGraphInstrumentor._deprecation_logged:
            LangGraphInstrumentor._deprecation_logged = True
            logger.warning(
                "LangGraphInstrumentor is deprecated and is now a no-op. LangGraph "
                "node/tool/LLM spans are captured automatically by LangChainInstrumentor; "
                "you can remove the LangGraphInstrumentor().instrument() call."
            )

    def _uninstrument(self, **kwargs: Any) -> None:
        self._is_instrumented = False

    @property
    def is_instrumented(self) -> bool:
        return self._is_instrumented
