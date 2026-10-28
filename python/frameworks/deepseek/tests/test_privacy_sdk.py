"""Exercise real OpenAI parsing, including DeepSeek reasoning and tool streams."""
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_deepseek import DeepSeekInstrumentor

# Use the transport accepted by the installed OpenAI SDK (1.x and 3.x).
from openai._base_client import httpx


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("hide_outputs", [False, True])
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
async def test_reasoning_and_tool_output_privacy(
    monkeypatch, asynchronous, streaming, hide_outputs, configuration
):
    answer = "PRIVATE_ANSWER_3481"
    reasoning = "PRIVATE_REASONING_4729"
    arguments = '{"secret":"PRIVATE_TOOL_ARGUMENTS_6823"}'
    tool = {"id": "call-offline", "type": "function", "function": {"name": "lookup", "arguments": arguments}}
    usage = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8,
             "completion_tokens_details": {"reasoning_tokens": 2}}
    message = {"role": "assistant", "content": answer, "reasoning_content": reasoning, "tool_calls": [tool]}
    base = {"id": "offline-completion", "created": 0, "model": "deepseek-reasoner"}
    body = {**base, "object": "chat.completion", "choices": [
        {"index": 0, "message": message, "finish_reason": "tool_calls"}
    ], "usage": usage}
    chunks = []
    # Split reasoning and tool arguments to verify reconstruction, not just one chunk.
    deltas = [
        {"role": "assistant", "reasoning_content": reasoning[:10]},
        {"reasoning_content": reasoning[10:], "content": answer,
         "tool_calls": [{"index": 0, **tool, "function": {"name": "lookup", "arguments": arguments[:12]}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": arguments[12:]}}]},
    ]
    for delta in deltas:
        chunks.append({**base, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": delta, "finish_reason": None}
        ]})
    chunks.append({**base, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
    ], "usage": usage})
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if streaming:
            data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            return httpx.Response(200, content=data + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=body)

    monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())
    config = None
    if configuration == "explicit":
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(not hide_outputs).lower())
        config = TraceConfig(hide_outputs=hide_outputs)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = DeepSeekInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        kwargs = dict(model="deepseek-reasoner", stream=streaming, messages=[
            {"role": "system", "content": "System instructions"},
            {"role": "user", "content": "Offline question"},
        ])
        if asynchronous:
            async with openai.AsyncOpenAI(api_key="offline-placeholder", base_url="https://api.deepseek.com/v1",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
                response = await client.chat.completions.create(**kwargs)
                if streaming:
                    received = [chunk.model_dump(exclude_none=True) async for chunk in response]
                else:
                    received = response.model_dump()
        else:
            with openai.OpenAI(api_key="offline-placeholder", base_url="https://api.deepseek.com/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
                response = client.chat.completions.create(**kwargs)
                if streaming:
                    received = [chunk.model_dump(exclude_none=True) for chunk in response]
                else:
                    received = response.model_dump()
        assert len(requests) == 1
        assert requests[0]["messages"][0]["role"] == "system"
        if streaming:
            assert len(received) == len(chunks)
            assert "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in received) == reasoning
            assert "".join(t["function"].get("arguments", "") for c in received
                           for t in c["choices"][0]["delta"].get("tool_calls", [])) == arguments
            assert received[1]["choices"][0]["delta"]["content"] == answer
        else:
            assert received["choices"][0]["message"]["content"] == answer
            assert received["choices"][0]["message"]["reasoning_content"] == reasoning
            assert received["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == arguments
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = dict(spans[0].attributes)
        assert attributes["gen_ai.usage.output_tokens"] == 5
        assert attributes["deepseek.reasoning_tokens"] == 2
        assert attributes["deepseek.tool_calls_count"] == 1
        if hide_outputs:
            assert attributes["output.value"] == REDACTED_VALUE
            assert not any(key.startswith("gen_ai.output.messages") for key in attributes)
            for private in (answer, reasoning, arguments, "PRIVATE_TOOL_ARGUMENTS_6823"):
                assert private not in json.dumps(attributes)
        else:
            assert attributes["deepseek.reasoning_content"] == reasoning
            assert attributes["deepseek.output_reasoning"] == reasoning
            assert attributes["deepseek.tool_calls.0.function.arguments"] == arguments
            assert attributes["gen_ai.output.messages.0.message.content"] == answer
            payload = json.loads(attributes["output.value"])
            output = payload if streaming else payload["choices"][0]["message"]
            assert output["content"] == answer
            assert output["reasoning_content"] == reasoning
            assert output["tool_calls"][0]["function"]["arguments"] == arguments
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
