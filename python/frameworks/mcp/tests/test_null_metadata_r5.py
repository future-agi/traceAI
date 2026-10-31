import asyncio
from contextlib import nullcontext

import pytest
from mcp.shared.message import SessionMessage
from mcp.types import CallToolRequestParams, JSONRPCMessage, JSONRPCRequest
from opentelemetry import context, trace

from traceai_mcp import InstrumentedStreamWriter


class Sink:
    async def send(self, item):
        self.item = item
        return "sent"


def envelope(request):
    try:
        return SessionMessage(JSONRPCMessage(root=request))
    except TypeError:
        return SessionMessage(request)


@pytest.mark.parametrize("active_span", (False, True))
def test_optional_null_metadata_is_safe_and_caller_owned(active_span):
    asyncio.run(check_optional_metadata(active_span))


async def check_optional_metadata(active_span):
    params = {"name": "echo", "arguments": {"value": "hello"}, "_meta": None}
    assert CallToolRequestParams.model_validate(params).meta is None
    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/call", params=params)
    item = envelope(request)
    sink = Sink()
    writer = InstrumentedStreamWriter(sink)
    token = context.attach(context.Context())
    span = trace.NonRecordingSpan(
        trace.SpanContext(42, 43, False, trace.TraceFlags(1), trace.TraceState())
    )
    try:
        with trace.use_span(span) if active_span else nullcontext():
            assert await writer.send(item) == "sent"
    finally:
        context.detach(token)
    assert request.params["_meta"] is None
    assert params["_meta"] is None
    assert request.params["arguments"] is params["arguments"]
    sent_request = getattr(sink.item.message, "root", sink.item.message)
    if active_span:
        assert sink.item is not item
        assert sent_request.params["_meta"]["traceparent"] == (
            "00-0000000000000000000000000000002a-000000000000002b-01"
        )
    else:
        assert sink.item is item
        assert sent_request.params["_meta"] is None


def test_no_propagation_does_not_add_empty_metadata():
    asyncio.run(check_no_propagation())


async def check_no_propagation():
    request = JSONRPCRequest(
        jsonrpc="2.0", id=1, method="tools/call", params={"name": "echo"}
    )
    item = envelope(request)
    sink = Sink()
    token = context.attach(context.Context())
    try:
        assert await InstrumentedStreamWriter(sink).send(item) == "sent"
    finally:
        context.detach(token)
    assert sink.item is item
    assert "_meta" not in request.params
