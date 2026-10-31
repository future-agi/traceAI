"""Cerebras compatible-client attribution via the real SDK, with no network."""

import httpx
import openai
import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

from .test_openai_compatible_spans import (
    REQUEST_MODEL,
    async_client,
    chat_completion,
    chat_stream,
    client,
    only_span,
    unauthorized,
)
from .test_openai_compatible_spans import (
    make_tracer as make_tracer,  # noqa: PLC0414 - register the shared pytest fixture
)


@pytest.mark.parametrize(
    "base_url,provider",
    [
        ("https://api.cerebras.ai/v1", "cerebras"),
        ("https://API.Cerebras.ai./v1", "cerebras"),
        ("https://api.cerebras.ai.example.com/v1", "openai"),
        ("https://proxy.api.cerebras.ai/v1", "openai"),
    ],
)
@pytest.mark.parametrize("mode", ["completion", "stream", "error"])
def test_cerebras_provider(make_tracer, base_url, provider, mode):
    exporter = make_tracer()
    handler = {
        "completion": lambda _: httpx.Response(200, json=chat_completion()),
        "stream": lambda _: chat_stream(),
        "error": unauthorized,
    }[mode]
    with client(handler, base_url=base_url) as sdk:
        kwargs = {
            "model": REQUEST_MODEL,
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": mode == "stream",
        }
        if mode == "error":
            with pytest.raises(openai.AuthenticationError):
                sdk.chat.completions.create(**kwargs)
        else:
            result = sdk.chat.completions.create(**kwargs)
            if mode == "stream":
                list(result)
    span, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == provider
    assert provider.encode() in encode_spans([span]).SerializeToString()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream", "error"])
async def test_async_cerebras_provider(make_tracer, mode):
    exporter = make_tracer()
    handler = {
        "completion": lambda _: httpx.Response(200, json=chat_completion()),
        "stream": lambda _: chat_stream(),
        "error": unauthorized,
    }[mode]
    async with async_client(handler, base_url="https://api.cerebras.ai/v1") as sdk:
        kwargs = {
            "model": REQUEST_MODEL,
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": mode == "stream",
        }
        if mode == "error":
            with pytest.raises(openai.AuthenticationError):
                await sdk.chat.completions.create(**kwargs)
        else:
            result = await sdk.chat.completions.create(**kwargs)
            if mode == "stream":
                async for _ in result:
                    pass
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == "cerebras"
