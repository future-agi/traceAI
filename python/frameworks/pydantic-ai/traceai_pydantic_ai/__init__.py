"""TraceAI instrumentation for Pydantic AI.

This package provides OpenTelemetry instrumentation for Pydantic AI,
enabling comprehensive tracing of agent executions, tool calls, and
model interactions.

Example:
    from traceai_pydantic_ai import PydanticAIInstrumentor

    # Initialize instrumentation
    PydanticAIInstrumentor().instrument()

    # Use Pydantic AI as normal - tracing is automatic
    from pydantic_ai import Agent

    agent = Agent('openai:gpt-4o', instructions='Be concise.')
    result = agent.run_sync('What is 2 + 2?')
    print(result.output)
"""

from traceai_pydantic_ai._instrumentor import (
    PydanticAIInstrumentor,
    instrument_pydantic_ai,
)
from traceai_pydantic_ai._attributes import (
    PydanticAIAttributes,
    PydanticAISpanKind,
    get_model_provider,
)
from traceai_pydantic_ai._agent_wrapper import (
    wrap_agent_run,
    wrap_tool_function,
)

__version__ = "0.1.0"

__all__ = [
    # Main API
    "PydanticAIInstrumentor",
    "instrument_pydantic_ai",
    # Attributes
    "PydanticAIAttributes",
    "PydanticAISpanKind",
    "get_model_provider",
    # Advanced: Wrappers
    "wrap_agent_run",
    "wrap_tool_function",
]
