"""TraceAI instrumentation for IBM BeeAI Framework.

This module provides integration between TraceAI and IBM's BeeAI Framework,
enabling comprehensive observability for AI agent workflows.

BeeAI has built-in OpenTelemetry support via the openinference-instrumentation-beeai
package. This integration provides:
1. A convenience function to configure BeeAI with TraceAI's endpoint
2. Helper functions for trace attribute management
3. Extended middleware for additional observability

Example usage:

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from traceai_beeai import configure_beeai_tracing

    # Setup TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="beeai-agent",
    )

    # Configure BeeAI to use TraceAI (call BEFORE importing beeai)
    configure_beeai_tracing(tracer_provider=trace_provider)

    # Now import and use BeeAI normally
    from beeai_framework.agents import Agent
    from beeai_framework.tools import WikipediaTool
"""

from traceai_beeai._instrumentor import (
    configure_beeai_tracing,
    BeeAIInstrumentorWrapper,
)
from traceai_beeai._attributes import (
    SpanAttributes,
    get_agent_attributes,
    get_tool_attributes,
    get_workflow_attributes,
    get_model_provider,
)
from traceai_beeai._middleware import (
    TraceAIMiddleware,
    create_tracing_middleware,
)

__all__ = [
    "configure_beeai_tracing",
    "BeeAIInstrumentorWrapper",
    "SpanAttributes",
    "get_agent_attributes",
    "get_tool_attributes",
    "get_workflow_attributes",
    "get_model_provider",
    "TraceAIMiddleware",
    "create_tracing_middleware",
]

__version__ = "0.1.0"
