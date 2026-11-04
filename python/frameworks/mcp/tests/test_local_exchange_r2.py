"""End-to-end local stdio exchanges with genuine MCP clients and servers."""
import asyncio
import json
from pathlib import Path
from importlib.metadata import version
import sys

from mcp import ClientSession, StdioServerParameters
import mcp.client.stdio as stdio
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from traceai_mcp import MCPInstrumentor


def test_real_client_server_exchange_context_concurrency_and_failure():
    async def run(traced):
        instrumentor = MCPInstrumentor()
        if traced:
            instrumentor.instrument(skip_dep_check=True)
        args = ['-I', str(Path(__file__).with_name('local_server_r2.py'))]
        if traced:
            args.append('--traced')
        try:
            async with stdio.stdio_client(StdioServerParameters(command=sys.executable, args=args)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {'echo', 'fail'} <= {tool.name for tool in tools.tools}
                    async def one(n):
                        span = NonRecordingSpan(SpanContext(n, n + 100, False, TraceFlags(1)))
                        token = context.attach(trace.set_span_in_context(span))
                        try:
                            result = await session.call_tool('echo', {'value': str(n)})
                            assert not getattr(result, 'is_error', getattr(result, 'isError', False))
                            body = json.loads(result.content[0].text)
                            assert body['value'] == str(n)
                            # MCP 2.x propagates OTel context natively; 1.x
                            # relies on the instrumentor for this transport.
                            native_propagation = int(version('mcp').split('.')[0]) >= 2
                            assert body['trace_id'] == (n if traced or native_propagation else 0)
                            assert trace.get_current_span().get_span_context().trace_id == n
                            return body['value']
                        finally:
                            context.detach(token)
                    values = await asyncio.gather(*(one(n) for n in range(1, 6)))
                    failure = await session.call_tool('fail', {})
                    assert getattr(failure, 'is_error', getattr(failure, 'isError', False))
                    failure_text = failure.content[0].text
                    assert 'fail' in failure_text.lower()
                    assert not trace.get_current_span().get_span_context().is_valid
                    return values, failure_text
        finally:
            if traced:
                instrumentor.uninstrument()
    untraced = asyncio.run(run(False))
    traced = asyncio.run(run(True))
    assert traced == untraced
    assert traced[0] == ['1', '2', '3', '4', '5']
