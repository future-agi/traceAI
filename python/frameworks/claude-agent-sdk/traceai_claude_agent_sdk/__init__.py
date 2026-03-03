"""TraceAI instrumentation for Claude Agent SDK.

This package provides OpenTelemetry instrumentation for the Claude Agent SDK
(formerly Claude Code SDK), enabling comprehensive tracing of AI agent
conversations, tool executions, and subagent coordination.

Example:
    from traceai_claude_agent_sdk import ClaudeAgentInstrumentor

    # Initialize instrumentation
    ClaudeAgentInstrumentor().instrument()

    # Use Claude Agent SDK as normal - tracing is automatic
    import asyncio
    from claude_agent_sdk import query, ClaudeAgentOptions

    async def main():
        async for message in query(
            prompt="What files are in this directory?",
            options=ClaudeAgentOptions(allowed_tools=["Glob", "Read"])
        ):
            print(message)

    asyncio.run(main())
"""

from traceai_claude_agent_sdk._instrumentor import (
    ClaudeAgentInstrumentor,
    instrument_claude_agent_sdk,
)
from traceai_claude_agent_sdk._attributes import (
    ClaudeAgentAttributes,
    ClaudeAgentSpanKind,
)
from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker
from traceai_claude_agent_sdk._session_tracker import SessionTracker
from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

__version__ = "0.1.0"

__all__ = [
    # Main API
    "ClaudeAgentInstrumentor",
    "instrument_claude_agent_sdk",
    # Attributes
    "ClaudeAgentAttributes",
    "ClaudeAgentSpanKind",
    # Advanced: Trackers
    "SubagentTracker",
    "SessionTracker",
    "MCPTracker",
]
