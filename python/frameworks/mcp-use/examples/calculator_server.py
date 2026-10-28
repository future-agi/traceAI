"""A one-tool MCP server over stdio, for examples/agent_with_tools.py."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calculator")


@mcp.tool()
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


if __name__ == "__main__":
    mcp.run("stdio")
