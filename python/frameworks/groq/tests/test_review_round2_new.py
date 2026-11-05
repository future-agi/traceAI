"""Synthetic transport probes, no vendor requests."""

import json
from contextlib import nullcontext

import httpx
import pytest
import test_stream_lifecycle as h
from groq import AsyncGroq, Groq

COMPLETION = {
    "id": "synthetic",
    "object": "chat.completion",
    "created": 0,
    "model": "response-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "answer"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("tracing_enabled", [False, True])
@pytest.mark.parametrize("field", ["tools", "message_content", "nested_tool_calls"])
async def test_one_shot_input_not_consumed(asynchronous, tracing_enabled, field):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=COMPLETION)

    params = {
        "model": "synthetic-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    if field == "tools":
        params["tools"] = iter(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "synthetic",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )
        expected = ["tools", 0, "function", "name"]
        wanted = "lookup"
    elif field == "message_content":
        params["messages"] = [
            {"role": "user", "content": iter([{"type": "text", "text": "hello"}])}
        ]
        expected = ["messages", 0, "content", 0, "text"]
        wanted = "hello"
    else:
        params["messages"] = [
            {
                "role": "assistant",
                "tool_calls": iter(
                    [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ]
                ),
            }
        ]
        expected = ["messages", 0, "tool_calls", 0, "function", "name"]
        wanted = "lookup"
    with h.instrumented() if tracing_enabled else nullcontext():
        if asynchronous:
            async with AsyncGroq(
                api_key="synthetic",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ) as client:
                completion = await client.chat.completions.create(**params)
        else:
            with Groq(
                api_key="synthetic",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as client:
                completion = client.chat.completions.create(**params)
        assert completion.choices[0].message.content == "answer"
    value = seen[0]
    if tracing_enabled:
        if field == "tools":
            assert value["tools"], f"tools on wire: {value['tools']}"
        elif field == "message_content":
            assert value["messages"][0]["content"], (
                f"content on wire: {value['messages'][0]['content']}"
            )
        else:
            assert value["messages"][0]["tool_calls"], (
                f"tool_calls on wire: {value['messages'][0]['tool_calls']}"
            )
    for key in expected:
        value = value[key]
    assert value == wanted, seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
@pytest.mark.parametrize("reader", ["read", "text", "json"])
async def test_nonstream_raw_prefetch_then_parse_retains_completion(
    asynchronous, api, reader
):
    def handler(req):
        return httpx.Response(200, json=COMPLETION)

    with h.instrumented() as (memory, provider):
        params = {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        if asynchronous:
            async with AsyncGroq(
                api_key="synthetic",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ) as client:
                resource = getattr(client.chat.completions, api)
                if api == "with_streaming_response":
                    async with resource.create(**params) as response:
                        assert await getattr(response, reader)()
                        parsed = await response.parse()
                else:
                    response = await resource.create(**params)
                    assert await getattr(response, reader)()
                    parsed = await response.parse()
        else:
            with Groq(
                api_key="synthetic",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as client:
                resource = getattr(client.chat.completions, api)
                if api == "with_streaming_response":
                    with resource.create(**params) as response:
                        assert getattr(response, reader)()
                        parsed = response.parse()
                else:
                    response = resource.create(**params)
                    assert getattr(response, reader)()
                    parsed = response.parse()
        assert parsed.choices[0].message.content == "answer"
        provider.force_flush()
        spans = memory.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("output.value") == "answer", dict(
            spans[0].attributes
        )
        assert spans[0].attributes.get("gen_ai.usage.total_tokens") == 5
        assert spans[0].attributes.get("gen_ai.response.model") == "response-model"
