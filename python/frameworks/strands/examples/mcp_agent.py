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
from fi_instrumentation.otel import HTTPSpanExporter
from traceai_strands import StrandsSpanExporter
from traceai_strands import create_traced_agent

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="strands-mcp-agent",
    # set_global_tracer_provider=True is required: Strands creates its spans from
    # OpenTelemetry's global tracer provider, so without it no Strands span reaches
    # this provider and nothing is sent to Future AGI.
    set_global_tracer_provider=True,
    span_exporter=StrandsSpanExporter(HTTPSpanExporter(), capture_content=True),
)

# Strands emits native spans; no additional telemetry exporter is needed.

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
    # To add your own stdio MCP server, keep its client context open while the
    # Agent runs. See the commented native-client setup below.

    print("=" * 60)
    print("Strands Agent with MCP Tools Demo")
    print("=" * 60)

    # Example with MCP client (requires running MCP server)
    # Uncomment and configure for your MCP server

    # from mcp import StdioServerParameters
    # from mcp.client.stdio import stdio_client
    #
    # parameters = StdioServerParameters(command="python", args=["your_server.py"])
    # with MCPClient(lambda: stdio_client(parameters)) as mcp_client:
    #     agent = create_traced_agent(
    #         model=YOUR_STRANDS_MODEL,
    #         tools=mcp_client.list_tools_sync(),
    #     )
    #     print(agent("Use the server's tools to answer my question."))

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
    print("The agent run has completed. MCP tools require the client setup above.")
    print("View the traces in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    finally:
        trace_provider.force_flush()
        trace_provider.shutdown()
