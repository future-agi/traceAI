"""TraceAI instrumentation for Agno Agent Framework.

This module provides integration between TraceAI and the Agno agent framework,
enabling comprehensive observability for AI agent workflows.

Agno has built-in OpenTelemetry support via the openinference-instrumentation-agno
package. This integration provides:
1. A convenience function to configure Agno with TraceAI's endpoint
2. Helper functions for trace attribute management
3. Session and user tracking utilities

Example usage:

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from traceai_agno import configure_agno_tracing

    # Setup TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="agno-agent",
    )

    # Configure Agno to use TraceAI (call BEFORE creating agents)
    configure_agno_tracing(tracer_provider=trace_provider)

    # Now import and use Agno normally
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
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

__version__ = "0.1.0"
