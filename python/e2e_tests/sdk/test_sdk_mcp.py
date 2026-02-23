"""
E2E Tests for MCP (Model Context Protocol) SDK Instrumentation

Tests MCP instrumentation. Requires an MCP server setup.
"""

import pytest
import os
import time

from config import config


HAS_MCP_SERVER = bool(os.getenv("MCP_SERVER_URL"))

skip_if_no_mcp = pytest.mark.skipif(
    not HAS_MCP_SERVER, reason="MCP_SERVER_URL not set"
)


@pytest.fixture(scope="module")
def setup_mcp():
    """Set up MCP with instrumentation."""
    if not HAS_MCP_SERVER:
        pytest.skip("MCP_SERVER_URL not set")

    from fi_instrumentation import register
    try:
        from traceai_mcp import MCPInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_mcp not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    MCPInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    MCPInstrumentor().uninstrument()


@skip_if_no_mcp
class TestMCPOperations:
    """Test MCP operations."""

    @pytest.mark.asyncio
    async def test_mcp_tool_call(self, setup_mcp):
        """Test MCP tool call."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client, StdioServerParameters

        server_url = os.getenv("MCP_SERVER_URL")

        # This is a basic test structure - actual implementation
        # depends on the MCP server being used
        server_params = StdioServerParameters(
            command="npx",
            args=["-y", server_url],
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # List available tools
                tools = await session.list_tools()
                assert len(tools.tools) > 0

                time.sleep(2)
                print(f"MCP tools: {[t.name for t in tools.tools]}")

    @pytest.mark.asyncio
    async def test_mcp_resource_read(self, setup_mcp):
        """Test MCP resource read."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client, StdioServerParameters

        server_url = os.getenv("MCP_SERVER_URL")

        server_params = StdioServerParameters(
            command="npx",
            args=["-y", server_url],
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # List resources
                resources = await session.list_resources()
                print(f"MCP resources: {len(resources.resources)} available")
