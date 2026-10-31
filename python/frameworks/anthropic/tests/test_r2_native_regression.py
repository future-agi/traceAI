"""Anthropic SDK offline regressions; no vendor calls are made."""
import asyncio
import json

import anthropic
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_anthropic import AnthropicInstrumentor

try:
    import httpx2 as httpx
except ImportError:
    import httpx

MESSAGE = {"id": "msg_local", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "answer"}], "model": "local", "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 4, "output_tokens": 2}}
START = {"id": "msg_local", "type": "message", "role": "assistant", "content": [], "model": "local", "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 4, "output_tokens": 0}}
EVENTS = [{"type": "message_start", "message": START}, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}, {"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}, {"type": "message_stop"}]
BODY = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in EVENTS)
PARAMS = {"model": "local", "max_tokens": 16, "messages": [{"role": "user", "content": "hello"}]}
TOOLS = [{"role": "assistant", "content": [{"type": "tool_use", "id": "tool1", "name": "lookup", "input": {"query": "abc"}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool1", "content": "ok"}]}]


def setup():
    p = TracerProvider()
    x = InMemorySpanExporter()
    p.add_span_processor(SimpleSpanProcessor(x))
    i = AnthropicInstrumentor()
    i.instrument(tracer_provider=p)
    return p, x, i


def handler(req):
    if json.loads(req.content).get("stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=BODY)
    return httpx.Response(200, json=MESSAGE)


def test_native_tool_mapping_sync():
    c = anthropic.Anthropic(api_key="local", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    params = dict(PARAMS, messages=TOOLS)
    expected = c.messages.create(**params).model_dump()
    p, x, i = setup()
    try:
        assert c.messages.create(**params).model_dump() == expected
        assert len(x.get_finished_spans()) == 1
    finally:
        i.uninstrument(); c.close(); p.shutdown()


def test_native_tool_mapping_async():
    async def run():
        c = anthropic.AsyncAnthropic(api_key="local", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        params = dict(PARAMS, messages=TOOLS)
        expected = (await c.messages.create(**params)).model_dump()
        p, x, i = setup()
        try:
            assert (await c.messages.create(**params)).model_dump() == expected
            assert len(x.get_finished_spans()) == 1
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["full", "close", "context"])
def test_native_sync_stream_lifecycle(mode):
    p, x, i = setup()
    c = anthropic.Anthropic(api_key="local", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        s = c.messages.create(**PARAMS, stream=True)
        if mode == "full":
            assert len(list(s)) == len(EVENTS)
        elif mode == "close":
            assert s.close() is None
            assert s.close() is None
        else:
            with s as entered:
                assert entered is s
                assert next(iter(entered)).type == "message_start"
        assert len(x.get_finished_spans()) == 1
    finally:
        i.uninstrument(); c.close(); p.shutdown()


@pytest.mark.parametrize("mode", ["full", "close", "context"])
def test_native_async_stream_lifecycle(mode):
    async def run():
        p, x, i = setup()
        c = anthropic.AsyncAnthropic(api_key="local", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            s = await c.messages.create(**PARAMS, stream=True)
            if mode == "full":
                assert len([e async for e in s]) == len(EVENTS)
            elif mode == "close":
                assert await s.close() is None
                assert await s.close() is None
            else:
                async with s as entered:
                    assert entered is s
                    assert (await entered.__aiter__().__anext__()).type == "message_start"
            assert len(x.get_finished_spans()) == 1
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    asyncio.run(run())


def test_native_typed_tool_use():
    block = anthropic.types.ToolUseBlock(id="tool1", name="lookup", input={"query": "abc"}, type="tool_use")
    params = dict(PARAMS, messages=[{"role": "assistant", "content": [block]}, TOOLS[1]])
    c = anthropic.Anthropic(api_key="local", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    expected = c.messages.create(**params).model_dump()
    p, x, i = setup()
    try:
        assert c.messages.create(**params).model_dump() == expected
        assert len(x.get_finished_spans()) == 1
    finally:
        i.uninstrument(); c.close(); p.shutdown()


def test_native_stream_error_is_hidden(monkeypatch, caplog):
    secret = "R2_ERROR_SECRET_831"
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    body = 'event: error\ndata: ' + json.dumps({"type": "error", "error": {"type": "api_error", "message": secret}}) + '\n\n'
    c = anthropic.Anthropic(api_key="local", http_client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body))))
    p, x, i = setup()
    try:
        with pytest.raises(Exception, match=secret):
            list(c.messages.create(**PARAMS, stream=True))
        spans = x.get_finished_spans()
        assert len(spans) == 1
        serialized = str(spans[0].status) + str(spans[0].attributes) + str([(e.name, dict(e.attributes)) for e in spans[0].events]) + caplog.text
        assert secret not in serialized
    finally:
        i.uninstrument(); c.close(); p.shutdown()


def test_native_async_stream_cancel():
    async def run():
        entered = asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""
        def slow(req):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=WaitingStream())
        c = anthropic.AsyncAnthropic(api_key="local", http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
        p, x, i = setup()
        try:
            s = await c.messages.create(**PARAMS, stream=True)
            async def consume():
                async for _ in s:
                    pass
            task = asyncio.create_task(consume())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(x.get_finished_spans()) == 1
            await s.close()
            assert len(x.get_finished_spans()) == 1
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    asyncio.run(run())
