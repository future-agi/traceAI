"""Transport aliases must share one propagation layer when they delegate."""
import asyncio
from contextlib import asynccontextmanager
from types import ModuleType
from unittest.mock import patch

import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest
from opentelemetry import propagate, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from traceai_mcp import (
    InstrumentedStreamReader,
    InstrumentedStreamWriter,
    MCPInstrumentor,
)


@pytest.mark.parametrize("arity", [2, 3])
@pytest.mark.parametrize("entrypoint", ["streamable_http_client", "streamablehttp_client"])
def test_delegating_alias_propagates_once(arity, entrypoint):
    module = ModuleType("delegating_alias_fixture")
    sent = []
    callback = lambda: "session-id"

    class Reader:
        def __aiter__(self):
            async def receive():
                yield sent[-1]
            return receive()

    class Writer:
        async def send(self, item):
            sent.append(item)

    reader, writer = Reader(), Writer()
    inner_streams = []
    exits = []

    @asynccontextmanager
    async def transport():
        try:
            streams = (reader, writer)
            yield streams if arity == 2 else (*streams, callback)
        finally:
            exits.append("transport")

    @asynccontextmanager
    async def legacy_alias():
        async with module.streamable_http_client() as streams:
            inner_streams.append(streams)
            yield streams

    module.streamable_http_client = transport
    module.streamablehttp_client = legacy_alias
    instrumentor = MCPInstrumentor()
    hooks = {}

    async def run():
        async with getattr(module, entrypoint)() as streams:
            assert len(streams) == arity
            if arity == 3:
                assert streams[2] is callback
            assert isinstance(streams[0], InstrumentedStreamReader)
            assert isinstance(streams[1], InstrumentedStreamWriter)
            assert streams[0].__wrapped__ is reader
            assert streams[1].__wrapped__ is writer
            if inner_streams:
                assert streams[0] is inner_streams[0][0]
                assert streams[1] is inner_streams[0][1]
            request = JSONRPCRequest(jsonrpc="2.0", id=1, method="ping")
            try:
                message = JSONRPCMessage(root=request)
            except TypeError:  # MCP 2.x exposes a union type alias instead.
                message = request
            item = SessionMessage(message)
            span = NonRecordingSpan(SpanContext(31, 32, False, TraceFlags(1)))
            propagator = propagate.get_global_textmap()
            with patch.object(propagator, "inject", wraps=propagator.inject) as inject:
                with trace.use_span(span):
                    await streams[1].send(item)
                inject.assert_called_once()
            assert request.params is None
            with patch("traceai_mcp.propagate.extract", wraps=propagate.extract) as extract:
                iterator = streams[0].__aiter__()
                try:
                    received = await iterator.__anext__()
                    assert received is sent[-1]
                    assert trace.get_current_span().get_span_context().trace_id == 31
                    extract.assert_called_once()
                finally:
                    await iterator.aclose()
            assert not trace.get_current_span().get_span_context().is_valid
            instrumentor._uninstrument()
            await streams[1].send(item)
            assert sent[-1] is item

    with patch.object(instrumentor, "_pending_hooks", set(), create=True), patch(
        "traceai_mcp.register_post_import_hook",
        side_effect=lambda fn, name: hooks.setdefault(name, fn),
    ):
        instrumentor._instrument()
        hooks["mcp.client.streamable_http"](module)
        try:
            asyncio.run(run())
        finally:
            instrumentor._uninstrument()
    assert exits == ["transport"]
    assert module.streamable_http_client is transport
    assert module.streamablehttp_client is legacy_alias


def test_stream_deduplication_preserves_foreign_lifecycle_state():
    instrumentor = MCPInstrumentor()
    own_state, foreign_state = object(), object()
    with patch.object(instrumentor, "_stream_state", own_state, create=True):
        for proxy_type in (InstrumentedStreamReader, InstrumentedStreamWriter):
            native = object()
            owned = proxy_type(native, own_state)
            assert instrumentor._wrap_stream(owned, proxy_type) is owned
            foreign = proxy_type(native, foreign_state)
            wrapped = instrumentor._wrap_stream(foreign, proxy_type)
            assert wrapped is not foreign
            assert wrapped.__wrapped__ is foreign
            assert wrapped._self_state is own_state
            assert foreign._self_state is foreign_state
