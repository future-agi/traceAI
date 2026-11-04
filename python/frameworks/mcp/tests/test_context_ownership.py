"""Stream/transport exit, not async-generator GC, owns propagation cleanup."""
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from contextvars import copy_context
from pathlib import Path

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from traceai_mcp import (
    ContextAttachingStreamReader,
    InstrumentedStreamReader,
    ItemWithContext,
    MCPInstrumentor,
)

REMOTE_TRACE = 0x12345678901234567890123456789012
REMOTE_SPAN = 0x1234567812345678
REMOTE_CONTEXT = trace.set_span_in_context(
    NonRecordingSpan(SpanContext(REMOTE_TRACE, REMOTE_SPAN, True, TraceFlags(1)))
)
TRACEPARENT = f"00-{REMOTE_TRACE:032x}-{REMOTE_SPAN:016x}-01"


@pytest.fixture(params=["asyncio", "trio"])
def backend(request):
    if request.param == "trio":
        pytest.importorskip("trio")
    return request.param


def message(reader_type):
    if reader_type is ContextAttachingStreamReader:
        return ItemWithContext("request", REMOTE_CONTEXT)
    request = JSONRPCRequest(
        jsonrpc="2.0", id=1, method="ping", params={"_meta": {"traceparent": TRACEPARENT}}
    )
    try:
        wrapped = JSONRPCMessage(root=request)
    except TypeError:  # MCP 2.x uses a union rather than a root model.
        wrapped = request
    return SessionMessage(wrapped)


@pytest.mark.parametrize("reason", ["break", "cancel"])
@pytest.mark.parametrize("close", ["explicit", "transport"])
def test_real_stdio_transport_restores_parent(reason, close):
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "context-test", "version": "1"},
            "_meta": {"traceparent": TRACEPARENT},
        },
    }
    result = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).with_name("local_context_server.py")), reason, close],
        input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Failed to detach context" not in result.stderr


@pytest.mark.parametrize("reader_type", [InstrumentedStreamReader, ContextAttachingStreamReader])
@pytest.mark.parametrize("reason", ["break", "cancel"])
@pytest.mark.parametrize("close", ["aclose", "close", "context"])
def test_reader_exit_restores_parent(reader_type, reason, close, caplog, backend):
    async def run():
        tracer = TracerProvider().get_tracer("reader-lifecycle")
        send, receive = anyio.create_memory_object_stream(1)
        reader = reader_type(receive)
        await send.send(message(reader_type))
        with tracer.start_as_current_span("outer") as outer:
            async def consume(stream):
                with anyio.CancelScope() as scope:
                    async for _ in stream:
                        assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
                        if reason == "cancel":
                            scope.cancel()
                            await anyio.sleep(0)
                            raise AssertionError("cancellation was not delivered")
                        break

            if close == "context":
                async with reader as stream:
                    await consume(stream)
            else:
                await consume(reader)
                if close == "aclose":
                    await reader.aclose()
                else:
                    reader.close()
            with tracer.start_as_current_span("independent") as independent:
                assert independent.get_span_context().trace_id == outer.get_span_context().trace_id
                assert independent.parent.span_id == outer.get_span_context().span_id
            # Repeated cleanup must neither detach twice nor overwrite later work.
            with tracer.start_as_current_span("later") as later:
                await reader.aclose()
                assert trace.get_current_span() is later
        await send.aclose()
    anyio.run(run, backend=backend)
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("reader_type", [InstrumentedStreamReader, ContextAttachingStreamReader])
def test_other_task_close_does_not_steal_context_cleanup(reader_type, caplog, backend):
    async def run():
        send, receive = anyio.create_memory_object_stream(1)
        reader = reader_type(receive)
        await send.send(message(reader_type))
        original = context.get_current()
        async for _ in reader:
            assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
            break
        # Task groups inherit context, but ContextVar tokens still belong to
        # the consuming task. A child closing the native stream cannot reset them.
        async with anyio.create_task_group() as group:
            group.start_soon(reader.aclose)
        assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
        await reader.aclose()
        assert context.get_current() is original
        await send.aclose()
    anyio.run(run, backend=backend)
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("reader_type", [InstrumentedStreamReader, ContextAttachingStreamReader])
def test_next_receive_cancellation_and_explicit_iterator_close(reader_type, caplog, backend):
    async def run():
        send, receive = anyio.create_memory_object_stream(2)
        reader = reader_type(receive)
        await send.send(message(reader_type))
        original = context.get_current()
        iterator = reader.__aiter__()
        await anext(iterator)
        assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
        # The next receive restores the previous item before waiting. Its
        # cancellation must not leave either remote context or a stale token.
        with anyio.move_on_after(0.02) as scope:
            await anext(iterator)
        assert scope.cancel_called
        assert context.get_current() is original
        iterator = reader.__aiter__()
        await send.send(message(reader_type))
        await anext(iterator)
        await iterator.aclose()
        assert context.get_current() is original
        await reader.aclose()
        await send.aclose()
    anyio.run(run, backend=backend)
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("reader_type", [InstrumentedStreamReader, ContextAttachingStreamReader])
def test_copied_context_cannot_detach_owner_tokens(reader_type, caplog, backend):
    async def run():
        send, receive = anyio.create_memory_object_stream(1)
        reader = reader_type(receive)
        await send.send(message(reader_type))
        original = context.get_current()
        async for _ in reader:
            break
        inherited = copy_context()
        stale = copy_context()
        inherited.run(reader.close)
        assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
        reader.close()
        assert context.get_current() is original
        # The copied Context also cannot reset a token already used by its owner.
        stale.run(reader.close)
        assert context.get_current() is original
        await send.aclose()
    anyio.run(run, backend=backend)
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("reader_type", [InstrumentedStreamReader, ContextAttachingStreamReader])
def test_fresh_iterator_restores_previous_message(reader_type, backend):
    async def run():
        send, receive = anyio.create_memory_object_stream(2)
        reader = reader_type(receive)
        await send.send(message(reader_type))
        plain = ValueError("transport exception value without context")
        await send.send(plain)
        original = context.get_current()
        first = reader.__aiter__()
        await anext(first)
        assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
        async for item in reader:
            assert item is plain
            assert context.get_current() is original
            break
        await first.aclose()
        await reader.aclose()
        await send.aclose()
    anyio.run(run, backend=backend)


@pytest.mark.parametrize("reason", ["break", "cancel"])
def test_mcp1_server_session_exit_restores_queued_context(reason, caplog, backend):
    from mcp.server import Server
    from mcp.server.session import ServerSession

    if not hasattr(ServerSession, "incoming_messages"):
        pytest.skip("MCP 2.x per-request sessions have no incoming-message queue")
    options = {"init_options": Server("context-test").create_initialization_options()}

    async def run():
        instrumentor = MCPInstrumentor()
        instrumentor.instrument()
        client_send, server_receive = anyio.create_memory_object_stream(1)
        server_send, client_receive = anyio.create_memory_object_stream(1)
        original = context.get_current()
        tracer = TracerProvider().get_tracer("session-lifecycle")
        try:
            with anyio.fail_after(5), tracer.start_as_current_span("outer") as outer:
                async with ServerSession(server_receive, server_send, **options) as session:
                    async with anyio.create_task_group() as group:
                        restore = context.attach(REMOTE_CONTEXT)
                        try:
                            group.start_soon(session._incoming_message_stream_writer.send, "request")
                        finally:
                            context.detach(restore)
                        with anyio.CancelScope() as scope:
                            async for item in session.incoming_messages:
                                assert item == "request"
                                assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
                                if reason == "cancel":
                                    scope.cancel()
                                    await anyio.sleep(0)
                                    raise AssertionError("cancellation was not delivered")
                                break
                with tracer.start_as_current_span("after_session") as after:
                    assert after.get_span_context().trace_id == outer.get_span_context().trace_id
                    assert after.parent.span_id == outer.get_span_context().span_id
            assert context.get_current() is original
        finally:
            instrumentor.uninstrument()
            await client_send.aclose()
            await client_receive.aclose()
    anyio.run(run, backend=backend)
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("arity", [2, 3])
def test_callback_transport_exit_restores_parent(arity, backend):
    async def run():
        send, receive = anyio.create_memory_object_stream(1)
        await send.send(message(InstrumentedStreamReader))
        callback = lambda: "synthetic-session"

        @asynccontextmanager
        async def transport():
            async with send, receive:
                yield (receive, send) if arity == 2 else (receive, send, callback)

        instrumentor = MCPInstrumentor()
        instrumentor.instrument()
        try:
            original = context.get_current()
            async with instrumentor._wrap_transport_with_callback(transport, None, (), {}) as streams:
                if arity == 3:
                    assert streams[2] is callback
                async for _ in streams[0]:
                    assert trace.get_current_span().get_span_context().trace_id == REMOTE_TRACE
                    break
            assert context.get_current() is original
        finally:
            instrumentor.uninstrument()
    anyio.run(run, backend=backend)
