"""Provider override and async raw-response parsing with real SDK MockTransport."""

import asyncio

import openai
import pytest
from traceai_openai import set_client_provider

from .test_deepseek_route import REASONING, attrs, httpx, respond, wire


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_explicit_provider_enables_deepseek_extraction(wire, asynchronous, streaming):
    spans = wire("shared")
    params = dict(model="deepseek-reasoner", messages=[{"role": "user", "content": "hello"}], stream=streaming)

    async def run():
        async with openai.AsyncOpenAI(
            api_key="synthetic-key", base_url="https://proxy.example/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            set_client_provider(client, provider="deepseek")
            result = await client.chat.completions.create(**params)
            if streaming:
                async with result:
                    async for _ in result:
                        pass

    if asynchronous:
        asyncio.run(run())
    else:
        with openai.OpenAI(
            api_key="synthetic-key", base_url="https://proxy.example/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client:
            set_client_provider(client, provider="deepseek")
            result = client.chat.completions.create(**params)
            if streaming:
                with result:
                    for _ in result:
                        pass
    a = attrs(spans)
    assert a["gen_ai.provider.name"] == "deepseek"
    assert a["deepseek.reasoning_content"] == REASONING
    assert a["deepseek.prompt_cache_hit_tokens"] == 3


@pytest.mark.parametrize("raw_mode", ["with_streaming_response", "with_raw_response"])
@pytest.mark.parametrize("streaming", [False, True])
def test_async_raw_response_parses_before_export(wire, raw_mode, streaming):
    spans = wire("shared")
    params = dict(model="deepseek-reasoner", messages=[{"role": "user", "content": "hello"}], stream=streaming)

    async def run():
        async with openai.AsyncOpenAI(
            api_key="synthetic-key", base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            if raw_mode == "with_streaming_response":
                async with client.chat.completions.with_streaming_response.create(**params) as raw:
                    assert not spans
                    result = await raw.parse()
                    if streaming:
                        async for _ in result:
                            pass
            else:
                raw = await client.chat.completions.with_raw_response.create(**params)
                result = raw.parse()
                if streaming:
                    async for _ in result:
                        pass
            assert result is not None

    asyncio.run(run())
    a = attrs(spans)
    assert a["deepseek.reasoning_content"] == REASONING
    assert a["gen_ai.usage.output_tokens"] == 7
