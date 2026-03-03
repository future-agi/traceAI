"""TraceAI instrumentation for AWS Strands Agents.

This module provides integration between TraceAI and AWS Strands Agents SDK,
enabling comprehensive observability for AI agent workflows.

Strands has built-in OpenTelemetry support, so this integration provides:
1. A convenience function to configure Strands with TraceAI's endpoint
2. Optional callback handlers for extended event capture
3. Helper functions for trace attribute management

Example usage:

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from traceai_strands import configure_strands_tracing, StrandsCallbackHandler

    # Setup TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="strands-agent",
    )

    # Configure Strands to use TraceAI
    configure_strands_tracing(tracer_provider=trace_provider)

    # Or use with callback handler for extended events
    from strands import Agent

    callback = StrandsCallbackHandler(tracer_provider=trace_provider)
    agent = Agent(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        callback_handler=callback,
    )
"""

from traceai_strands._instrumentor import (
    configure_strands_tracing,
    StrandsInstrumentor,
)
from traceai_strands._callback import StrandsCallbackHandler
from traceai_strands._attributes import (
    get_agent_attributes,
    get_tool_attributes,
    get_model_attributes,
)

__all__ = [
    "configure_strands_tracing",
    "StrandsInstrumentor",
    "StrandsCallbackHandler",
    "get_agent_attributes",
    "get_tool_attributes",
    "get_model_attributes",
]

__version__ = "0.1.0"
