"""Real MCP server with instrumentor-only propagation counters for lifecycle tests."""
import asyncio
import json
from importlib.metadata import version
from opentelemetry import trace
import traceai_mcp
from traceai_mcp import MCPInstrumentor

class Tap:
    def __init__(self, original):
        self.original = original
        self.injects = self.extracts = 0
    def get_global_textmap(self):
        original = self.original.get_global_textmap()
        tap = self
        class Injector:
            def inject(self, *args, **kwargs):
                tap.injects += 1
                return original.inject(*args, **kwargs)
        return Injector()
    def extract(self, *args, **kwargs):
        self.extracts += 1
        return self.original.extract(*args, **kwargs)

tap = Tap(traceai_mcp.propagate)
traceai_mcp.propagate = tap
instrumentor = MCPInstrumentor()
instrumentor.instrument(skip_dep_check=True)
if int(version('mcp').split('.')[0]) >= 2:
    from mcp.server.mcpserver import MCPServer as Server
else:
    from mcp.server.fastmcp import FastMCP as Server
server = Server('local-r3')
started = asyncio.Event()
release = asyncio.Event()

@server.tool()
async def control(action: str) -> str:
    if action == 'wait':
        started.set()
        await asyncio.wait_for(release.wait(), 8)
    elif action == 'release':
        release.set()
    elif action == 'off':
        instrumentor.uninstrument()
    elif action == 'on':
        instrumentor.instrument(skip_dep_check=True)
    return json.dumps({'action': action, 'trace_id': trace.get_current_span().get_span_context().trace_id,
                       'started': started.is_set(), 'extracts': tap.extracts, 'injects': tap.injects})

if __name__ == '__main__':
    server.run(transport='stdio')
