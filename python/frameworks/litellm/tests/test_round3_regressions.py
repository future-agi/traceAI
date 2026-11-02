"""Independent review probes. Only synthetic SDK transports and local OTel export."""
import json
from contextlib import contextmanager

import httpx
import litellm
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_litellm import LiteLLMInstrumentor


@contextmanager
def capture(config=None):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LiteLLMInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def attrs(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return dict(spans[0].attributes)


def response_payload():
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "synthetic-private-response"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("from_environment", [False, True])
@pytest.mark.asyncio
async def test_embedding_text_hide_flags(flag, asynchronous, from_environment, monkeypatch):
    inputs = ["synthetic-private-first", "synthetic-private-second"]
    payload = {
        "object": "list", "model": "text-embedding-ada-002",
        "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2]} for i in range(2)],
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    if from_environment:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**{flag: True})
    with capture(config) as exporter:
        if asynchronous:
            async with openai.AsyncOpenAI(api_key="synthetic-key", http_client=httpx.AsyncClient(transport=transport)) as client:
                result = await litellm.aembedding("text-embedding-ada-002", inputs, client=client)
        else:
            with openai.OpenAI(api_key="synthetic-key", http_client=httpx.Client(transport=transport)) as client:
                result = litellm.embedding("text-embedding-ada-002", inputs, client=client)
    assert len(result.data) == 2
    actual = attrs(exporter)
    for i, text in enumerate(inputs):
        key = f"embedding.embeddings.{i}.embedding.text"
        assert actual.get(key) != text, {"flag": flag, "key": key, "actual": actual.get(key)}


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_audio_and_text_input_both_preserved(with_tool, asynchronous):
    marker = "c3ludGhldGljLWF1ZGlv"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "What was said in this recording?"},
        {"type": "input_audio", "input_audio": {"data": marker, "format": "wav"}},
    ]}]
    if with_tool:
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_synthetic", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_synthetic", "content": "ready"},
        ]
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload())
    transport = httpx.MockTransport(respond)
    with capture() as exporter:
        kwargs = dict(model="openai/gpt-audio", messages=messages)
        if asynchronous:
            async with openai.AsyncOpenAI(api_key="synthetic-key", http_client=httpx.AsyncClient(transport=transport)) as client:
                result = await litellm.acompletion(**kwargs, client=client)
        else:
            with openai.OpenAI(api_key="synthetic-key", http_client=httpx.Client(transport=transport)) as client:
                result = litellm.completion(**kwargs, client=client)
    assert result.choices[0].message.content == "synthetic-private-response"
    assert marker in json.dumps(requests[0])
    actual = attrs(exporter)
    assert marker in json.dumps(actual), {"with_tool": with_tool, "actual": actual}

@pytest.mark.parametrize("flag", ["hide_input_text", "hide_input_messages"])
@pytest.mark.parametrize("kind", ["completion", "audio", "embedding"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_message_input_flags_do_not_leak_aggregate(flag, kind, asynchronous):
    marker = "synthetic-private-input-aaa"
    with capture(TraceConfig(**{flag: True})) as exporter:
        if kind == "embedding":
            payload = {"object": "list", "model": "text-embedding-ada-002", "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}]}
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
            if asynchronous:
                async with openai.AsyncOpenAI(api_key="synthetic-key", http_client=httpx.AsyncClient(transport=transport)) as client:
                    await litellm.aembedding("text-embedding-ada-002", marker, client=client)
            else:
                with openai.OpenAI(api_key="synthetic-key", http_client=httpx.Client(transport=transport)) as client:
                    litellm.embedding("text-embedding-ada-002", marker, client=client)
        else:
            content = marker if kind == "completion" else [{"type": "text", "text": marker}, {"type": "input_audio", "input_audio": {"data": "c3ludGhldGljLWF1ZGlv", "format": "wav"}}]
            kwargs = dict(model="gpt-4o-mini", messages=[{"role": "user", "content": content}], mock_response="ok", api_key="synthetic-key")
            if asynchronous:
                await litellm.acompletion(**kwargs)
            else:
                litellm.completion(**kwargs)
    actual = attrs(exporter)
    assert marker not in actual.get("input.value", ""), (flag, kind, actual.get("input.value"))


@pytest.mark.parametrize("flag", ["hide_output_text", "hide_output_messages"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_message_output_flags_do_not_leak_aggregate(flag, asynchronous, streaming):
    marker = "synthetic-private-output-bbb"
    kwargs = dict(model="gpt-4o-mini", messages=[{"role": "user", "content": "hello"}], mock_response=marker, api_key="synthetic-key", stream=streaming)
    with capture(TraceConfig(**{flag: True})) as exporter:
        if asynchronous:
            result = await litellm.acompletion(**kwargs)
            if streaming:
                async for _ in result:
                    pass
        else:
            result = litellm.completion(**kwargs)
            if streaming:
                list(result)
    actual = attrs(exporter)
    assert marker not in actual.get("output.value", ""), (flag, streaming, actual.get("output.value"))
