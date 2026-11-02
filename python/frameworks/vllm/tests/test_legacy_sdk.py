"""Keep the standalone legacy entry point usable at OpenAI 1.0 and current."""

import asyncio
import json
import sys

import openai
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_vllm import VLLMInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("operation", ["chat", "stream", "error"])
def test_real_sdk_without_shared_package(use_async, operation):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = VLLMInstrumentor()
    assert instrumentor.instrumentation_dependencies() == ("openai >= 1.0.0",)
    shared_was_loaded = "traceai_openai" in sys.modules

    def handle(request):
        if operation == "error":
            return httpx.Response(401, json={"error": {"message": "Synthetic error"}})
        response = {
            "id": "c1",
            "object": "chat.completion",
            "created": 1,
            "model": "test-vllm-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Synthetic output",
                    },
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
        if operation == "stream":
            response["object"] = "chat.completion.chunk"
            response["choices"][0]["delta"] = response["choices"][0].pop("message")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=response)

    options = {
        "base_url": "http://localhost:8000/v1",
        "api_key": "synthetic-not-a-real-key",
        "max_retries": 0,
    }
    params = {
        "model": "test-vllm-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": operation == "stream",
    }

    async def run_async():
        async with openai.AsyncOpenAI(
            **options,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            result = await client.chat.completions.create(**params)
            if operation == "stream":
                assert [chunk async for chunk in result]
            else:
                assert result.choices[0].message.content == "Synthetic output"

    def run_sync():
        with openai.OpenAI(
            **options,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            result = client.chat.completions.create(**params)
            if operation == "stream":
                assert list(result)
            else:
                assert result.choices[0].message.content == "Synthetic output"

    try:
        with pytest.warns(DeprecationWarning, match="OpenAIInstrumentor"):
            instrumentor.instrument(tracer_provider=provider)
        if operation == "error":
            with pytest.raises(openai.AuthenticationError):
                asyncio.run(run_async()) if use_async else run_sync()
        else:
            asyncio.run(run_async()) if use_async else run_sync()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["gen_ai.provider.name"] == "vllm"
        assert spans[0].attributes["gen_ai.request.model"] == "test-vllm-model"
        assert spans[0].status.is_ok == (operation != "error")
        assert "synthetic-not-a-real-key" not in str(spans[0].attributes)
        # Instrumentation must not load a package incompatible with the old SDK.
        assert ("traceai_openai" in sys.modules) == shared_was_loaded
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
