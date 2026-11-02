"""Real SDK regressions using only synthetic HTTP transports."""
import json
from contextlib import nullcontext

import httpx
import pytest
from groq import AsyncGroq, Groq
from test_stream_lifecycle import AsyncBody, PARAMS, SyncBody, async_client, instrumented, sync_client


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method", ["read", "text"])
async def test_raw_fully_read_finishes(asynchronous, method):
    body = (AsyncBody if asynchronous else SyncBody)()
    with instrumented() as (memory, provider):
        if asynchronous:
            async with async_client(body) as client:
                response = await client.chat.completions.with_raw_response.create(**PARAMS)
                assert await getattr(response, method)()
                assert response.is_closed
                stream = await response.parse()
                assert [chunk async for chunk in stream]
        else:
            with sync_client(body) as client:
                response = client.chat.completions.with_raw_response.create(**PARAMS)
                assert getattr(response, method)()
                assert response.is_closed
                stream = response.parse()
                assert list(stream)
        provider.force_flush()
        assert body.closes == 1
        spans = memory.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["output.value"] == "private-output-marker-tail"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method", ["read", "text"])
async def test_raw_fully_read_without_parse_finishes(asynchronous, method):
    body = (AsyncBody if asynchronous else SyncBody)()
    with instrumented() as (memory, provider):
        if asynchronous:
            async with async_client(body) as client:
                response = await client.chat.completions.with_raw_response.create(**PARAMS)
                assert await getattr(response, method)()
        else:
            with sync_client(body) as client:
                response = client.chat.completions.with_raw_response.create(**PARAMS)
                assert getattr(response, method)()
        provider.force_flush()
        assert response.is_closed and body.closes == 1
        assert len(memory.get_finished_spans()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
async def test_nonstream_raw_parsed_attributes(asynchronous, api):
    completion = {"id": "synthetic", "object": "chat.completion", "created": 0,
                  "model": "synthetic-response-model", "choices": [{"index": 0,
                  "message": {"role": "assistant", "content": "synthetic-response-content"},
                  "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    handler = lambda request: httpx.Response(200, json=completion)
    with instrumented() as (memory, _):
        params = dict(PARAMS, stream=False)
        if asynchronous:
            async with AsyncGroq(api_key="synthetic", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
                resource = getattr(client.chat.completions, api)
                if api == "with_streaming_response":
                    async with resource.create(**params) as response:
                        parsed = await response.parse()
                else:
                    response = await resource.create(**params)
                    parsed = await response.parse()
                    await response.close()
        else:
            with Groq(api_key="synthetic", http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                resource = getattr(client.chat.completions, api)
                if api == "with_streaming_response":
                    with resource.create(**params) as response:
                        parsed = response.parse()
                else:
                    response = resource.create(**params)
                    parsed = response.parse()
                    response.close()
        assert parsed.choices[0].message.content == "synthetic-response-content"
        span, = memory.get_finished_spans()
        assert span.attributes.get("output.value") == "synthetic-response-content"
        assert span.attributes.get("gen_ai.usage.total_tokens") == 5
        assert span.attributes.get("gen_ai.response.model") == "synthetic-response-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("tracing_enabled", [False, True])
async def test_message_iterator_preserved(asynchronous, tracing_enabled):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=(AsyncBody if asynchronous else SyncBody)(), headers={"content-type": "text/event-stream"})
    with (instrumented() if tracing_enabled else nullcontext()):
        params = dict(PARAMS, messages=iter(PARAMS["messages"]))
        if asynchronous:
            async with AsyncGroq(api_key="synthetic", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
                stream = await client.chat.completions.create(**params)
                _ = [chunk async for chunk in stream]
        else:
            with Groq(api_key="synthetic", http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                list(client.chat.completions.create(**params))
    assert requests[0]["messages"] == PARAMS["messages"]
