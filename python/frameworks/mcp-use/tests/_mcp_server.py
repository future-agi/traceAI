"""A fake MCP server over stdio for the traceAI-mcp-use tests.

mcp-use starts it as a subprocess (MCPClient "command"/"args"). It talks to
nothing else. Each tool returns a marker string so the tests can tell
whether tool content reached a span.
"""

import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def add(a: int, b: int) -> str:
    """Add two integers."""
    return "SUM-RESULT-{0}".format(a + b)


@mcp.tool()
def echo(text: str) -> str:
    """Return the text."""
    return "ECHO-RESULT:" + text


@mcp.tool()
def fail(reason: str) -> str:
    """Always fails; the MCP result has isError set."""
    raise ValueError("tool failed because " + reason)


@mcp.tool()
async def slow(seconds: float) -> str:
    """Sleep, then return."""
    await asyncio.sleep(seconds)
    return "SLEPT"


if __name__ == "__main__":
    mcp.run("stdio")
