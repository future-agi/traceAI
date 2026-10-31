"""Offline real MCP stdio server used by test_local_exchange_r2."""
import json
import sys
from importlib.metadata import version
from opentelemetry import trace

if '--traced' in sys.argv:
    from traceai_mcp import MCPInstrumentor
    MCPInstrumentor().instrument(skip_dep_check=True)

if int(version('mcp').split('.')[0]) >= 2:
    from mcp.server.mcpserver import MCPServer as Server
else:
    from mcp.server.fastmcp import FastMCP as Server

server = Server('local-r2')

@server.tool()
async def echo(value: str) -> str:
    span = trace.get_current_span().get_span_context()
    return json.dumps({'value': value, 'trace_id': span.trace_id})

@server.tool()
async def fail() -> str:
    raise ValueError('local-tool-failure')

if __name__ == '__main__':
    server.run(transport='stdio')
