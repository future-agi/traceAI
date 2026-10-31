"""Compatibility checks using the installed MCP SDK's actual message models."""
import asyncio

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCNotification
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from traceai_mcp import InstrumentedStreamReader, InstrumentedStreamWriter


def session_message(value):
    # The public union became a type alias in MCP 2.x.
    try:
        value = JSONRPCMessage(root=value)
    except TypeError:
        pass
    return SessionMessage(value)


def test_native_request_roundtrip_and_context():
    async def run():
        request = JSONRPCRequest(jsonrpc="2.0", id=1, method="ping")
        item = session_message(request)
        class Sink:
            async def send(self, value):
                return value
        span = NonRecordingSpan(SpanContext(0x123, 0x456, False, TraceFlags(1)))
        token = context.attach(trace.set_span_in_context(span))
        try:
            sent = await InstrumentedStreamWriter(Sink()).send(item)
        finally:
            context.detach(token)
        assert sent is not item
        assert request.params is None
        sent_request = getattr(sent.message, "root", sent.message)
        assert sent_request.params["_meta"]["traceparent"].endswith("0000000000000456-01")
        class Source:
            def __aiter__(self):
                async def generate():
                    yield sent
                return generate()
        async for received in InstrumentedStreamReader(Source()):
            assert received is sent
            assert trace.get_current_span().get_span_context().trace_id == 0x123
        assert not trace.get_current_span().get_span_context().is_valid
    asyncio.run(run())


def test_notification_remains_unchanged():
    async def run():
        item = session_message(JSONRPCNotification(jsonrpc="2.0", method="ready"))
        class Sink:
            async def send(self, value):
                return value
        assert await InstrumentedStreamWriter(Sink()).send(item) is item
    asyncio.run(run())
