"""Chat usage details reach spans without changing SDK results."""

import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_openai import OpenAIInstrumentor


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize(
    "details,cached,reasoning",
    [
        pytest.param(
            {
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
            80,
            10,
            id="nonzero",
        ),
        pytest.param(
            {
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
            0,
            0,
            id="zero",
        ),
        pytest.param({}, None, None, id="missing-details"),
        pytest.param(
            {"prompt_tokens_details": None, "completion_tokens_details": None},
            None,
            None,
            id="null-details",
        ),
        pytest.param(
            {"prompt_tokens_details": {}, "completion_tokens_details": {}},
            None,
            None,
            id="missing-counts",
        ),
        pytest.param(
            {
                "prompt_tokens_details": {"cached_tokens": None},
                "completion_tokens_details": {"reasoning_tokens": None},
            },
            None,
            None,
            id="null-counts",
        ),
        pytest.param(
            {"prompt_tokens_details": {"cached_tokens": 80}},
            80,
            None,
            id="cache-only",
        ),
        pytest.param(
            {"completion_tokens_details": {"reasoning_tokens": 10}},
            None,
            10,
            id="reasoning-only",
        ),
    ],
)
def test_chat_usage_details(exporter, use_async, stream, details, cached, reasoning):
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        **details,
    }
    common = {"id": "chatcmpl-test", "created": 1, "model": "gpt-4o-mini"}

    def handler(_request):
        if not stream:
            return httpx.Response(
                200,
                json={
                    **common,
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Hello"},
                    }],
                    "usage": usage,
                },
            )
        chunks = [
            {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Hello"},
                    "finish_reason": None,
                }],
                "usage": None,
            },
            {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": None,
            },
            {
                **common,
                "object": "chat.completion.chunk",
                "choices": [],
                "usage": usage,
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )

    kwargs = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }
    if stream:
        kwargs["stream_options"] = {"include_usage": True}
    client_kwargs = {
        "api_key": "test-only-key",
        "base_url": "https://api.openai.com/v1",
        "max_retries": 0,
    }

    async def run_async():
        async with openai.AsyncOpenAI(
            **client_kwargs,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        ) as client:
            response = await client.chat.completions.create(**kwargs)
            return [chunk async for chunk in response] if stream else response

    if use_async:
        result = asyncio.run(run_async())
    else:
        with openai.OpenAI(
            **client_kwargs,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        ) as client:
            response = client.chat.completions.create(**kwargs)
            result = list(response) if stream else response

    if stream:
        assert len(result) == 3
        assert result[-1].choices == []
        assert "".join(
            choice.delta.content or "" for chunk in result for choice in chunk.choices
        ) == "Hello"
        sdk_usage = result[-1].usage
    else:
        assert result.choices[0].message.content == "Hello"
        sdk_usage = result.usage
    assert sdk_usage.prompt_tokens == 100
    assert sdk_usage.completion_tokens == 20
    assert sdk_usage.total_tokens == 120
    assert getattr(sdk_usage.prompt_tokens_details, "cached_tokens", None) == cached
    assert getattr(sdk_usage.completion_tokens_details, "reasoning_tokens", None) == reasoning

    (span,) = exporter.get_finished_spans()
    expected = {
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 20,
        "gen_ai.usage.total_tokens": 120,
    }
    if cached is not None:
        expected["gen_ai.usage.input_tokens.cache_read"] = cached
    if reasoning is not None:
        expected["gen_ai.usage.output_tokens.reasoning"] = reasoning
    assert {
        key: value for key, value in span.attributes.items()
        if key.startswith("gen_ai.usage.")
    } == expected
