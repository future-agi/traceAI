"""Example showing subagent tracing with Claude Agent SDK.

This example demonstrates how TraceAI traces subagent (Task tool)
invocations, creating proper parent-child span relationships.
"""

import asyncio

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, BatchSpanProcessor

# Set up OpenTelemetry
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Initialize TraceAI instrumentation
from traceai_claude_agent_sdk import ClaudeAgentInstrumentor

ClaudeAgentInstrumentor().instrument(tracer_provider=provider)

try:
    from claude_agent_sdk import query, ClaudeAgentOptions

    async def main():
        """Run an agent that uses subagents."""
        print("Running Claude Agent with subagent tracing...\n")

        # This query will likely spawn subagents for complex tasks
        async for message in query(
            prompt="""
            I need you to analyze this project:
            1. First, explore the directory structure
            2. Then read the main configuration file
            3. Finally, summarize what this project does
            """,
            options=ClaudeAgentOptions(
                allowed_tools=["Glob", "Read", "Task"],
                max_turns=10,
            )
        ):
            msg_type = type(message).__name__

            if msg_type == "AssistantMessage":
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if hasattr(block, "text"):
                            print(f"Assistant: {block.text[:300]}...")
                        elif hasattr(block, "name"):
                            print(f"[Tool Use: {block.name}]")
            elif msg_type == "ResultMessage":
                print(f"\nResult: {message.usage.input_tokens} input, {message.usage.output_tokens} output tokens")
                if hasattr(message, "total_cost_usd"):
                    print(f"Cost: ${message.total_cost_usd:.4f}")

        print("\n--- Trace Summary ---")
        print("The trace shows:")
        print("1. Root span: claude_agent.conversation")
        print("2. Turn spans: claude_agent.assistant_turn (one per response)")
        print("3. Tool spans: claude_agent.tool (Glob, Read, Task)")
        print("4. Subagent spans: Nested under Task tool calls")

    asyncio.run(main())

except ImportError:
    print("Claude Agent SDK not installed.")
    print()
    print("When installed, this example shows how subagents are traced:")
    print()
    print("Span Hierarchy:")
    print("  claude_agent.conversation (root)")
    print("    └── claude_agent.assistant_turn")
    print("          └── claude_agent.tool (Task)")
    print("                └── claude_agent.subagent")
    print("                      └── claude_agent.assistant_turn")
    print("                            └── claude_agent.tool (Read)")
    print()
    print("Each subagent gets its own span with:")
    print("  - claude_agent.subagent.type: Agent type (Explore, Plan, etc.)")
    print("  - claude_agent.subagent.prompt: The task description")
    print("  - claude_agent.parent_tool_use_id: Link to parent Task tool")
