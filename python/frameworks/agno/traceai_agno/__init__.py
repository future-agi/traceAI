"""Future AGI export setup for the maintained OpenInference Agno instrumentor.

Configure once before running agents. Pass an existing provider explicitly, or
supply an OTLP traces endpoint to create one. The exported attribute/context
helpers are for manual use; they do not transform automatically emitted spans.
See the package README for authentication, content controls, and shutdown.
"""

from traceai_agno._instrumentor import (
    configure_agno_tracing,
    AgnoInstrumentorWrapper,
    setup_traceai_exporter,
)
from traceai_agno._attributes import (
    SpanAttributes,
    get_agent_attributes,
    get_tool_attributes,
    get_team_attributes,
    get_model_provider,
    create_trace_context,
)

__all__ = [
    "configure_agno_tracing",
    "AgnoInstrumentorWrapper",
    "setup_traceai_exporter",
    "SpanAttributes",
    "get_agent_attributes",
    "get_tool_attributes",
    "get_team_attributes",
    "get_model_provider",
    "create_trace_context",
]

__version__ = "0.1.0"  # x-release-please-version
