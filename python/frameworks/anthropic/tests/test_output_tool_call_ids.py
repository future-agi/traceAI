"""T2-ANTHROPIC-002: retain provider IDs for parallel calls to one tool."""
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


TOOL_CALLS = [
    {"type": "tool_use", "id": "call_fake_first", "name": "lookup", "input": {"q": "hello"}},
    {"type": "tool_use", "id": "call_fake_second", "name": "lookup", "input": {"q": "goodbye"}},
]
MESSAGE = {
    "id": "msg_fake", "type": "message", "role": "assistant", "model": "local-test",
    "content": [{"type": "text", "text": "Looking up both."}, *TOOL_CALLS],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 4, "output_tokens": 8},
}
PARAMS = {
    "model": "local-test", "max_tokens": 32,
    "messages": [{"role": "user", "content": "Look up hello and goodbye."}],
    "tools": [{
        "name": "lookup", "description": "Look up a query.",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }],
}


def stream_events():
    yield {
        "type": "message_start",
        "message": {**MESSAGE, "content": [], "stop_reason": None,
                    "usage": {"input_tokens": 4, "output_tokens": 0}},
    }
    yield {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "Looking up both."},
    }
    yield {"type": "content_block_stop", "index": 0}
    for index, call in enumerate(TOOL_CALLS, start=1):
        yield {
            "type": "content_block_start", "index": index,
            "content_block": {**call, "input": {}},
        }
        arguments = json.dumps(call["input"])
        # Two fragments ensure an ID is retained, not concatenated per delta.
        for fragment in (arguments[:6], arguments[6:]):
            yield {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": fragment},
            }
        yield {"type": "content_block_stop", "index": index}
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 8},
    }
    yield {"type": "message_stop"}


def transport_handler(request):
    if json.loads(request.content).get("stream"):
        payload = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
            for event in stream_events()
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload.encode()
        )
    return httpx.Response(200, json=MESSAGE)


def consume_sync(client, streaming):
    response = client.messages.create(**PARAMS, stream=streaming)
    if not streaming:
        return response.model_dump()
    try:
        return [event.model_dump() for event in response]
    finally:
        response.close()


async def consume_async(client, streaming):
    response = await client.messages.create(**PARAMS, stream=streaming)
    if not streaming:
        return response.model_dump()
    try:
        return [event.model_dump() async for event in response]
    finally:
        await response.close()


@pytest.mark.parametrize(
    "async_, streaming",
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["sync-message", "async-message", "sync-stream", "async-stream"],
)
def test_output_tool_call_ids(async_, streaming):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = AnthropicInstrumentor()

    async def run_async():
        async with anthropic.AsyncAnthropic(
            api_key="fake-local-key", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport_handler)),
        ) as client:
            expected = await consume_async(client, streaming)
            instrumentor.instrument(tracer_provider=provider)
            return expected, await consume_async(client, streaming)

    try:
        if async_:
            expected, actual = asyncio.run(run_async())
        else:
            with anthropic.Anthropic(
                api_key="fake-local-key", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(transport_handler)),
            ) as client:
                expected = consume_sync(client, streaming)
                instrumentor.instrument(tracer_provider=provider)
                actual = consume_sync(client, streaming)

        # Instrumentation must leave every SDK response/event unchanged.
        assert actual == expected
        if streaming:
            returned_ids = [
                event["content_block"]["id"] for event in actual
                if event["type"] == "content_block_start"
                and event["content_block"]["type"] == "tool_use"
            ]
        else:
            returned_ids = [block["id"] for block in actual["content"] if block["type"] == "tool_use"]
        assert returned_ids == [call["id"] for call in TOOL_CALLS]

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = spans[0].attributes
        assert attributes["gen_ai.output.messages.0.message.content"] == "Looking up both."
        for index, call in enumerate(TOOL_CALLS):
            prefix = f"gen_ai.output.messages.0.message.tool_calls.{index}.tool_call"
            assert attributes[f"{prefix}.function.name"] == call["name"]
            assert json.loads(attributes[f"{prefix}.function.arguments"]) == call["input"]
            assert attributes[f"{prefix}.id"] == call["id"]
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
