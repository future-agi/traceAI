"""Caller-owned message and pending-import lifecycle regressions."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import anyio
from mcp import StdioServerParameters
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCNotification, LATEST_PROTOCOL_VERSION
import mcp.client.stdio as stdio
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from traceai_mcp import MCPInstrumentor, InstrumentedStreamWriter, _StreamState


def envelope(request):
    try:
        return SessionMessage(JSONRPCMessage(root=request))
    except TypeError:
        return SessionMessage(request)


def attach(n):
    return context.attach(trace.set_span_in_context(NonRecordingSpan(SpanContext(n, n + 100, False, TraceFlags(1)))))


def request(n=17, params=None):
    return envelope(JSONRPCRequest(jsonrpc='2.0', id=n, method='tools/call', params=params or {'name': 'echo', 'arguments': {'value': 'same'}}))


def test_copy_path_concurrent_and_caller_metadata():
    async def run():
        state = _StreamState()
        seen = []
        sentinel = object()
        class Sink:
            async def send(self, item):
                await asyncio.sleep(0)
                seen.append(item)
                return sentinel
        writer = InstrumentedStreamWriter(Sink(), state)
        msg = request()
        original = deepcopy(msg)
        req = getattr(msg.message, 'root', msg.message)
        payload = req.params['arguments']
        async def send(n):
            token = attach(n)
            try:
                assert await writer.send(msg) is sentinel
            finally:
                context.detach(token)
        await asyncio.gather(send(401), send(402))
        assert msg == original
        assert {getattr(x.message, 'root', x.message).params['_meta']['traceparent'].split('-')[1] for x in seen} == {f'{401:032x}', f'{402:032x}'}
        for x in seen:
            assert type(x) is type(msg) and type(x.message) is type(msg.message)
            assert getattr(x.message, 'root', x.message).params['arguments'] is payload
            assert x.metadata is msg.metadata
        state.active = False
        await send(403)
        assert seen[-1] is msg and msg == original
        state.active = True
        explicit = request(params={'name': 'echo', 'arguments': payload, '_meta': {'traceparent': '00-00000000000000000000000000000001-0000000000000002-01', 'custom': payload}})
        snapshot = deepcopy(explicit)
        token = attach(404)
        try:
            await writer.send(explicit)
        finally:
            context.detach(token)
        assert explicit == snapshot
        assert getattr(seen[-1].message, 'root', seen[-1].message).params['_meta']['traceparent'].split('-')[1] == f'{1:032x}'
    asyncio.run(run())


def test_native_stdio_reused_request_and_disable():
    async def run():
        inst = MCPInstrumentor()
        inst.instrument(skip_dep_check=True)
        server = str(Path(__file__).with_name('local_server_r2.py'))
        params = StdioServerParameters(command=sys.executable, args=['-I', server, '--traced'])
        try:
            with anyio.fail_after(30):
                async with stdio.stdio_client(params) as (reader, writer):
                    iterator = reader.__aiter__()
                    async def response(n):
                        while True:
                            item = await anext(iterator)
                            if isinstance(item, Exception):
                                raise item
                            m = getattr(item.message, 'root', item.message)
                            if getattr(m, 'id', None) == n:
                                return m
                    await writer.send(envelope(JSONRPCRequest(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': LATEST_PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': {'name': 'reuse', 'version': '1'}})))
                    assert hasattr(await response(1), 'result')
                    await writer.send(envelope(JSONRPCNotification(jsonrpc='2.0', method='notifications/initialized')))
                    msg = request(); snapshot = deepcopy(msg)
                    for n in (401, 402, 403, 404):
                        if n == 403:
                            inst.uninstrument()
                        if n == 404:
                            inst.instrument(skip_dep_check=True)
                        token = attach(n)
                        try:
                            assert await writer.send(msg) is None
                        finally:
                            context.detach(token)
                        result = (await response(17)).result
                        data = result if isinstance(result, dict) else result.model_dump()
                        body = json.loads(data['content'][0]['text'])
                        assert body == {'value': 'same', 'trace_id': 0 if n == 403 else n}
                        assert msg == snapshot
                    await writer.aclose(); await iterator.aclose()
        finally:
            if inst.is_instrumented_by_opentelemetry:
                inst.uninstrument()
    asyncio.run(run())


def test_pending_hooks_shared_instances_and_import_while_disabled():
    script = '''
import sys
import wrapt.importer as hooks
from traceai_mcp import MCPInstrumentor
name = 'mcp.client.sse'
assert name not in sys.modules
one = MCPInstrumentor(); two = MCPInstrumentor()
assert one is two
for _ in range(100):
    one.instrument(skip_dep_check=True); two.uninstrument()
assert len(hooks._post_import_hooks.get(name, [])) == 1
import mcp.client.sse as module
original = module.sse_client
assert len(hooks._post_import_hooks.get(name, [])) == 0
one.instrument(skip_dep_check=True)
assert module.sse_client is not original
two.uninstrument()
assert module.sse_client is original
one.instrument(skip_dep_check=True)
assert module.sse_client is not original
one.uninstrument()
assert module.sse_client is original
'''
    subprocess.run([sys.executable, '-I', '-c', textwrap.dedent(script)], check=True, timeout=30)


def test_native_http_reused_request_and_disable():
    import socket
    import mcp.client.streamable_http as transport
    async def run():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        server = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('local_http_server_r4.py')), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        inst = MCPInstrumentor()
        try:
            with anyio.fail_after(30):
                while True:
                    if server.poll() is not None:
                        raise RuntimeError('loopback server exited')
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(.05)
                inst.instrument(skip_dep_check=True)
                factory = getattr(transport, 'streamable_http_client', None) or transport.streamablehttp_client
                async with factory(f'http://127.0.0.1:{port}/mcp') as streams:
                    reader, writer = streams[:2]
                    iterator = reader.__aiter__()
                    async def response(n):
                        while True:
                            item = await anext(iterator)
                            if isinstance(item, Exception):
                                raise item
                            m = getattr(item.message, 'root', item.message)
                            if getattr(m, 'id', None) == n:
                                return m
                    await writer.send(envelope(JSONRPCRequest(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': LATEST_PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': {'name': 'reuse-http', 'version': '1'}})))
                    assert hasattr(await response(1), 'result')
                    await writer.send(envelope(JSONRPCNotification(jsonrpc='2.0', method='notifications/initialized')))
                    msg = request(); snapshot = deepcopy(msg)
                    for n in (401, 402, 403, 404):
                        if n == 403:
                            inst.uninstrument()
                        if n == 404:
                            inst.instrument(skip_dep_check=True)
                        token = attach(n)
                        try:
                            assert await writer.send(msg) is None
                        finally:
                            context.detach(token)
                        result = (await response(17)).result
                        data = result if isinstance(result, dict) else result.model_dump()
                        assert json.loads(data['content'][0]['text']) == {'value': 'same', 'trace_id': 0 if n == 403 else n}
                        assert msg == snapshot
                    await writer.aclose()
                    await iterator.aclose()
        finally:
            if inst.is_instrumented_by_opentelemetry:
                inst.uninstrument()
            server.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(server.wait), 5)
            except asyncio.TimeoutError:
                server.kill()
                await asyncio.to_thread(server.wait)
    asyncio.run(run())
