"""Strands Agent with MCP (Model Context Protocol) tools example.

This example demonstrates:
- Using MCP servers with Strands Agents
- Tracing MCP tool execution
- Combining MCP and custom tools
"""

import os
from typing import Annotated

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_strands import configure_strands_tracing, create_traced_agent

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="strands-mcp-agent",
)

# Configure Strands to use TraceAI
configure_strands_tracing(tracer_provider=trace_provider)

# Import Strands (after configuration)
from strands import Agent, tool
from strands.tools.mcp import MCPClient


# Custom tool to complement MCP tools
@tool
def format_result(
    data: Annotated[str, "Data to format"],
    format_type: Annotated[str, "Format type: json, table, or summary"],
) -> str:
    """Format data in the specified format."""
    if format_type == "json":
        return f"```json\n{data}\n```"
    elif format_type == "table":
        return f"| Data |\n|------|\n| {data} |"
    else:
        return f"Summary: {data}"


def main():
    # Note: This example requires an MCP server running
    # You can use the MCP calculator server as an example:
    # npx @anthropic/mcp-server-calculator

    print("=" * 60)
    print("Strands Agent with MCP Tools Demo")
    print("=" * 60)

    # Example with MCP client (requires running MCP server)
    # Uncomment and configure for your MCP server

    # mcp_client = MCPClient(
    #     server_command=["npx", "@anthropic/mcp-server-calculator"],
    # )
    #
    # mcp_tools = mcp_client.list_tools_sync()
    # print(f"Available MCP tools: {[t.name for t in mcp_tools]}")

    # Create agent with traced attributes
    agent = create_traced_agent(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        system_prompt="""You are a helpful assistant with access to MCP tools.
        Use the calculator for math operations and format results nicely.""",
        tools=[format_result],  # Add mcp_tools here when MCP server is available
        session_id="mcp-demo-001",
        tags=["demo", "mcp"],
    )

    # Test queries
    queries = [
        "Calculate 25 times 4 and format the result as a summary.",
        "What is the square root of 144? Format as JSON.",
    ]

    for query in queries:
        print(f"\n{'-' * 60}")
        print(f"User: {query}")
        response = agent(query)
        print(f"Agent: {response}")

    print("\n" + "=" * 60)
    print("MCP tool calls have been traced!")
    print("View the traces in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
