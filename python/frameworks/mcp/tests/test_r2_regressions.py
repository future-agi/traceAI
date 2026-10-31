"""MCP SDK lifecycle and transport transparency regressions."""
import asyncio
import subprocess
import sys
import textwrap

import anyio
from traceai_mcp import InstrumentedStreamReader


def test_exception_item_identity():
    async def run():
        send, receive = anyio.create_memory_object_stream(1)
        error = ValueError('transport error')
        await send.send(error)
        await send.aclose()
        items = [item async for item in InstrumentedStreamReader(receive)]
        assert len(items) == 1 and items[0] is error
    asyncio.run(run())


def test_foreign_wrapper_survives_disable_and_reinstall():
    script = '''
import asyncio
import mcp.client.streamable_http as module
from wrapt import wrap_function_wrapper
from traceai_mcp import MCPInstrumentor, InstrumentedStreamReader
name = 'streamable_http_client' if hasattr(module, 'streamable_http_client') else 'streamablehttp_client'
i = MCPInstrumentor()
calls = []
def foreign(wrapped, instance, args, kwargs):
    calls.append(1)
    return wrapped(*args, **kwargs)
async def check(expected):
    async with getattr(module, name)('http://localhost:9/mcp') as streams:
        reader = streams[0]
        assert isinstance(reader, InstrumentedStreamReader) == expected
        if expected:
            assert not isinstance(reader.__wrapped__, InstrumentedStreamReader)
i.instrument(skip_dep_check=True)
wrap_function_wrapper(module, name, foreign)
foreign_installed = getattr(module, name)
i.uninstrument()
assert getattr(module, name) is foreign_installed
asyncio.run(check(False))
i.instrument(skip_dep_check=True)
asyncio.run(check(True))
i.uninstrument()
assert getattr(module, name) is foreign_installed
asyncio.run(check(False))
assert len(calls) == 3
'''
    subprocess.run([sys.executable, '-I', '-c', textwrap.dedent(script)], check=True, timeout=20)


def test_real_stdio_malformed_message_is_exception():
    # The SDK's stdio transport parses this invalid line and puts the actual
    # Pydantic parse exception on its receive stream rather than raising it.
    async def run(traced):
        from mcp import StdioServerParameters
        import mcp.client.stdio as stdio
        from traceai_mcp import MCPInstrumentor
        i = MCPInstrumentor()
        if traced:
            i.instrument(skip_dep_check=True)
        try:
            params = StdioServerParameters(command=sys.executable, args=['-I', '-c', "print('not-json', flush=True)"])
            async with stdio.stdio_client(params) as (reader, writer):
                with anyio.fail_after(5):
                    iterator = reader.__aiter__()
                    try:
                        item = await anext(iterator)
                    finally:
                        if hasattr(iterator, 'aclose'):
                            await iterator.aclose()
                await writer.aclose()
                assert isinstance(item, Exception), repr(item)
                return type(item).__name__
        finally:
            if traced:
                i.uninstrument()
    assert asyncio.run(run(False)) == asyncio.run(run(True))
