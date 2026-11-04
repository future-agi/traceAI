"""Independent traced loopback server for raw native HTTP message reuse."""
import json
import sys
from importlib.metadata import version
from opentelemetry import trace
from traceai_mcp import MCPInstrumentor
import uvicorn

MCPInstrumentor().instrument(skip_dep_check=True)
if version('mcp').startswith('1.'):
    from mcp.server.fastmcp import FastMCP as Server
else:
    from mcp.server.mcpserver import MCPServer as Server
server = Server('local-http-r4')

@server.tool()
async def echo(value: str) -> str:
    return json.dumps({'value': value, 'trace_id': trace.get_current_span().get_span_context().trace_id})

if __name__ == '__main__':
    uvicorn.run(server.streamable_http_app(), host='127.0.0.1', port=int(sys.argv[1]), log_level='error')
