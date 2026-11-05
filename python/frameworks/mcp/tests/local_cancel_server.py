"""Real MCP server for the cancellation-followed-by-healthy-request regression."""
import asyncio
from importlib.metadata import version
import json
from pathlib import Path
import sys

from traceai_mcp import MCPInstrumentor


def main():
    markers = Path(sys.argv[1])
    if sys.argv[2] == "instrumented":
        instrumentor = MCPInstrumentor()
        instrumentor.instrument()
        assert instrumentor.is_instrumented_by_opentelemetry

    native = int(version("mcp").split(".")[0]) >= 2
    if native:
        from mcp.server.mcpserver import MCPServer as Server
    else:
        from mcp.server.fastmcp import FastMCP as Server

    server = Server("cancellation-regression")

    @server.tool()
    async def hold() -> str:
        # MCP 1.x requires an explicit protocol cancellation; MCP 2.x sends it
        # when the client's call task is cancelled.
        request_id = None if native else server.get_context().request_context.request_id
        (markers / "started.json").write_text(json.dumps({"id": request_id}))
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            (markers / "cancelled").touch()
            raise
        return "unexpected completion"

    @server.tool()
    async def echo() -> str:
        return "healthy"

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
