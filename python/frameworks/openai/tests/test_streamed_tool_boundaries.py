"""Real-SDK regressions for fragmented names and OTel attribute eviction."""

import asyncio
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_openai import OpenAIInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx


def stream_body(tool_count, legacy):
    def delta(name, arguments, first=False):
        function = {"name": name, "arguments": arguments}
        if legacy:
            return {"function_call": function}
        return {
            "tool_calls": [
                {
                    "index": i,
                    **({"id": f"call_{i}", "type": "function"} if first else {}),
                    "function": function,
                }
                for i in range(tool_count)
            ]
        }

    events = [
        {"role": "assistant", **delta("get_", '{"city":"', first=True)},
        delta("weather", 'Paris"}'),
    ]
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "served-model",
            "choices": [{"index": 0, "delta": event, "finish_reason": None}],
        }
        for event in events
    ]
    chunks.append(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "served-model",
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
    )
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )


def collect(*, tool_count=1, legacy=False, private=False, use_async=False, limit=128):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_attributes=limit))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(hide_outputs=private)
    )

    def handle(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream_body(tool_count, legacy),
        )

    options = {
        "api_key": "synthetic-not-real",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "max_retries": 0,
    }
    params = {
        "model": "requested-model",
        "messages": [{"role": "user", "content": "Use weather tool"}],
        "stream": True,
    }

    async def run_async():
        async with openai.AsyncOpenAI(
            **options,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            stream = await client.chat.completions.create(**params)
            assert len([chunk async for chunk in stream]) == 3

    try:
        if use_async:
            asyncio.run(run_async())
        else:
            with openai.OpenAI(
                **options,
                http_client=httpx.Client(
                    transport=httpx.MockTransport(handle), trust_env=False
                ),
            ) as client:
                assert len(list(client.chat.completions.create(**params))) == 3
        provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return dict(spans[0].attributes)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("tool_count", [1, 50])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("limit", [16, 128])
def test_many_tools_keep_primary_attributes(use_async, tool_count, private, limit):
    attrs = collect(
        tool_count=tool_count, private=private, use_async=use_async, limit=limit
    )
    expected = {
        "gen_ai.provider.name": "fireworks",
        "gen_ai.request.model": "requested-model",
        "gen_ai.span.kind": "LLM",
        "gen_ai.usage.input_tokens": 5,
        "gen_ai.usage.output_tokens": 2,
        "gen_ai.usage.total_tokens": 7,
    }
    assert {key: attrs.get(key) for key in expected} == expected
    if private:
        assert "Paris" not in json.dumps(attrs)
        assert "get_weather" not in json.dumps(attrs)
    else:
        messages = json.loads(attrs["output.value"])
        assert len(messages[0]["tool_calls"]) == tool_count
        assert messages[0]["tool_calls"][-1]["function"] == {
            "name": "get_weather",
            "arguments": '{"city":"Paris"}',
        }


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_fragmented_function_names_are_concatenated(use_async, legacy):
    attrs = collect(use_async=use_async, legacy=legacy)
    prefix = "gen_ai.output.messages.0.message."
    if legacy:
        assert attrs[prefix + "function_call_name"] == "get_weather"
    else:
        assert attrs[prefix + "tool_calls.0.tool_call.function.name"] == "get_weather"
    message = json.loads(attrs["output.value"])[0]
    function = (
        message["function_call"] if legacy else message["tool_calls"][0]["function"]
    )
    assert function == {"name": "get_weather", "arguments": '{"city":"Paris"}'}
