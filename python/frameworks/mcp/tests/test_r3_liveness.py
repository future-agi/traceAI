"""Lifecycle regressions for streams already handed to native MCP sessions."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
import mcp.client.stdio as stdio
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from traceai_mcp import MCPInstrumentor, ContextSavingStreamWriter, ContextAttachingStreamReader, _StreamState
import traceai_mcp
from wrapt import wrap_function_wrapper


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


def test_queue_transitions_preserve_items_and_context():
    async def run():
        state = _StreamState()
        send, receive = anyio.create_memory_object_stream(4)
        writer = ContextSavingStreamWriter(send, state)
        reader = ContextAttachingStreamReader(receive, state)
        span = NonRecordingSpan(SpanContext(42, 43, False, TraceFlags(1)))
        token = context.attach(trace.set_span_in_context(span))
        try:
            await writer.send('enabled-before-disable')
            state.active = False
            await writer.send('disabled-before-reinstall')
        finally:
            context.detach(token)
        iterator = reader.__aiter__()
        assert await anext(iterator) == 'enabled-before-disable'
        assert not trace.get_current_span().get_span_context().is_valid
        state.active = True
        assert await anext(iterator) == 'disabled-before-reinstall'
        assert not trace.get_current_span().get_span_context().is_valid
        token = context.attach(trace.set_span_in_context(span))
        try:
            await writer.send('enabled-after-reinstall')
        finally:
            context.detach(token)
        assert await anext(iterator) == 'enabled-after-reinstall'
        assert trace.get_current_span().get_span_context().trace_id == 42
        await iterator.aclose()
        assert not trace.get_current_span().get_span_context().is_valid
        await send.aclose(); await receive.aclose()
    asyncio.run(run())


def test_open_native_client_and_server_lifecycle(monkeypatch):
    async def run():
        instrumentor = MCPInstrumentor()
        tap = Tap(traceai_mcp.propagate)
        monkeypatch.setattr(traceai_mcp, 'propagate', tap)
        operations = []
        original_send = traceai_mcp.InstrumentedStreamWriter.send
        async def observed_send(proxy, item):
            message = getattr(item, 'message', None)
            request = getattr(message, 'root', message)
            before = tap.injects
            active = proxy._self_state.active
            result = await original_send(proxy, item)
            if getattr(request, 'method', None) == 'tools/call':
                operations.append((active, tap.injects - before))
            return result
        monkeypatch.setattr(traceai_mcp.InstrumentedStreamWriter, 'send', observed_send)
        calls = []
        original_factory = stdio.stdio_client
        instrumentor.instrument(skip_dep_check=True)
        def foreign(wrapped, instance, args, kwargs):
            calls.append(1)
            return wrapped(*args, **kwargs)
        wrap_function_wrapper(stdio, 'stdio_client', foreign)
        foreign_installed = stdio.stdio_client
        params = StdioServerParameters(command=sys.executable, args=['-I', str(Path(__file__).with_name('local_server_r3.py'))])
        async def call(session, action, n):
            token = context.attach(trace.set_span_in_context(NonRecordingSpan(SpanContext(n, n+100, False, TraceFlags(1)))))
            try:
                with anyio.fail_after(8):
                    result = await session.call_tool('control', {'action': action})
                return json.loads(result.content[0].text)
            finally:
                context.detach(token)
        try:
            with anyio.fail_after(30):
                async with stdio.stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        a = await call(session, 'sample', 7)
                        assert operations[-1] == (True, 1)
                        instrumentor.uninstrument()
                        assert stdio.stdio_client is foreign_installed
                        start = tap.injects
                        b = await call(session, 'off', 8)
                        c = await call(session, 'sample', 9)
                        assert tap.injects == start
                        assert c['extracts'] == b['extracts']
                        instrumentor.instrument(skip_dep_check=True)
                        d = await call(session, 'on', 10)
                        e = await call(session, 'sample', 11)
                        assert operations[-2:] == [(True, 1), (True, 1)]
                        assert e['extracts'] == d['extracts'] + 1
                        instrumentor.uninstrument()
                        start = tap.injects
                        f = await call(session, 'off', 12)
                        g = await call(session, 'sample', 13)
                        assert tap.injects == start
                        assert g['extracts'] == f['extracts']
                        assert [x['action'] for x in (a,b,c,d,e,f,g)] == ['sample','off','sample','on','sample','off','sample']
                        from importlib.metadata import version
                        if version('mcp').startswith('1.'):
                            assert [x['trace_id'] for x in (a,c,e)] == [7, 0, 11]
                assert stdio.stdio_client is foreign_installed and len(calls) == 1
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            stdio.stdio_client = original_factory
    asyncio.run(run())


def test_concurrent_open_sessions_with_inflight_response():
    from contextlib import AsyncExitStack
    async def run():
        instrumentor = MCPInstrumentor()
        instrumentor.instrument(skip_dep_check=True)
        params = StdioServerParameters(command=sys.executable, args=['-I', str(Path(__file__).with_name('local_server_r3.py'))])
        async def call(session, action, n):
            token = context.attach(trace.set_span_in_context(NonRecordingSpan(SpanContext(n, n+100, False, TraceFlags(1)))))
            try:
                with anyio.fail_after(8):
                    result = await session.call_tool('control', {'action': action})
                assert trace.get_current_span().get_span_context().trace_id == n
                return json.loads(result.content[0].text)
            finally:
                context.detach(token)
        try:
            with anyio.fail_after(35):
                async with AsyncExitStack() as stack:
                    sessions = []
                    for _ in range(2):
                        reader, writer = await stack.enter_async_context(stdio.stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(reader, writer))
                        await session.initialize()
                        sessions.append(session)
                    pending = asyncio.create_task(call(sessions[0], 'wait', 21))
                    try:
                        while not (await call(sessions[0], 'status', 22))['started']:
                            await asyncio.sleep(.01)
                        await call(sessions[0], 'off', 23)
                        instrumentor.uninstrument()
                        disabled = await asyncio.gather(*(call(sessions[i % 2], 'sample', 30+i) for i in range(6)))
                        assert [x['action'] for x in disabled] == ['sample'] * 6
                        instrumentor.instrument(skip_dep_check=True)
                        await call(sessions[0], 'on', 40)
                        enabled = await asyncio.gather(*(call(sessions[i % 2], 'sample', 50+i) for i in range(6)))
                        assert [x['trace_id'] for x in enabled] == list(range(50, 56))
                        await call(sessions[0], 'release', 60)
                        answer = await pending
                        assert answer['action'] == 'wait' and answer['trace_id'] == 21
                    finally:
                        if not pending.done():
                            pending.cancel()
                            try:
                                await pending
                            except asyncio.CancelledError:
                                pass
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
    asyncio.run(run())


def test_caller_metadata_preserved_and_disabled_message_untouched():
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest
    from traceai_mcp import InstrumentedStreamWriter
    async def run():
        state = _StreamState()
        seen = []
        class Sink:
            async def send(self, item):
                seen.append(item)
                return item
        def item():
            request = JSONRPCRequest(jsonrpc='2.0', id=7, method='tools/call', params={
                'name': 'example', '_meta': {'custom': {'keep': True}, 'traceparent': '00-00000000000000000000000000000001-0000000000000002-01'}})
            try:
                message = JSONRPCMessage(root=request)
            except TypeError:
                message = request
            return request, SessionMessage(message)
        writer = InstrumentedStreamWriter(Sink(), state)
        span = NonRecordingSpan(SpanContext(42, 43, False, TraceFlags(1)))
        token = context.attach(trace.set_span_in_context(span))
        try:
            request, message = item()
            before = request.model_dump()
            assert await writer.send(message) is message
            assert request.model_dump() == before
            state.active = False
            request, message = item()
            before = request.model_dump()
            assert await writer.send(message) is message
            assert request.model_dump() == before
            assert seen[-1] is message
        finally:
            context.detach(token)
    asyncio.run(run())


def test_local_http_open_session_lifecycle():
    import socket
    import uvicorn
    from importlib.metadata import version
    if int(version('mcp').split('.')[0]) >= 2:
        from mcp.server.mcpserver import MCPServer as Server
    else:
        from mcp.server.fastmcp import FastMCP as Server
    import mcp.client.streamable_http as transport
    async def run():
        instrumentor = MCPInstrumentor()
        instrumentor.instrument(skip_dep_check=True)
        server = Server('local-http-r3')
        @server.tool()
        async def echo(value: str) -> str:
            return json.dumps({'value': value, 'trace_id': trace.get_current_span().get_span_context().trace_id})
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        uv = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level='error', lifespan='on'))
        server_task = asyncio.create_task(uv.serve(sockets=[sock]))
        async def call(session, n):
            token = context.attach(trace.set_span_in_context(NonRecordingSpan(SpanContext(n, n+100, False, TraceFlags(1)))))
            try:
                with anyio.fail_after(8):
                    result = await session.call_tool('echo', {'value': str(n)})
                return json.loads(result.content[0].text)
            finally:
                context.detach(token)
        try:
            with anyio.fail_after(30):
                while not uv.started:
                    if server_task.done():
                        await server_task
                        raise RuntimeError('HTTP server exited before startup')
                    await asyncio.sleep(.01)
                factory = getattr(transport, 'streamable_http_client', None) or transport.streamablehttp_client
                async with factory(f'http://127.0.0.1:{port}/mcp') as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        a = await call(session, 71)
                        instrumentor.uninstrument()
                        b = await call(session, 72)
                        instrumentor.instrument(skip_dep_check=True)
                        c = await call(session, 73)
                        assert [x['value'] for x in (a,b,c)] == ['71','72','73']
                        if version('mcp').startswith('1.'):
                            assert [x['trace_id'] for x in (a,b,c)] == [71, 0, 73]
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            uv.should_exit = True
            try:
                await asyncio.wait_for(server_task, 8)
            finally:
                sock.close()
    asyncio.run(run())
