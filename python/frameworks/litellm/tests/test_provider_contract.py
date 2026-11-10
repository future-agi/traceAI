"""Real LiteLLM/OpenAI objects with synthetic HTTP transports; no provider traffic."""

import copy
import json
from contextlib import contextmanager

import httpx
import litellm
import openai
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from litellm.types.utils import EmbeddingResponse, ModelResponse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import traceai_litellm
from traceai_litellm import LiteLLMInstrumentor

MESSAGES = [{"role": "user", "content": "synthetic-input"}]


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setattr(traceai_litellm, "Protect", None)

    @contextmanager
    def setup(config=None):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        inst = LiteLLMInstrumentor()
        inst.instrument(tracer_provider=provider, config=config or TraceConfig())
        try:
            yield exporter
        finally:
            inst.uninstrument()
            provider.shutdown()

    return setup


def attributes(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.OK
    return dict(spans[0].attributes)


class Bytes(httpx.SyncByteStream):
    def __init__(self):
        self.close_count = 0

    def __iter__(self):
        for text in ("first", "second"):
            obj = {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            }
            yield ("data: " + json.dumps(obj) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    def close(self):
        self.close_count += 1


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self):
        self.close_count = 0

    async def __aiter__(self):
        for chunk in Bytes():
            yield chunk

    async def aclose(self):
        self.close_count += 1


@pytest.mark.parametrize("consume_first", [False, True])
def test_real_sync_provider_stream_early_close(capture, consume_first):
    body = Bytes()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )
    )
    with openai.OpenAI(
        api_key="synthetic-key", http_client=httpx.Client(transport=transport)
    ) as client:
        with capture() as exporter:
            stream = litellm.completion(
                model="gpt-4o-mini", messages=MESSAGES, stream=True, client=client
            )
            if consume_first:
                next(stream)
            stream.close()
            stream.close()
            assert body.close_count == 1
            attributes(exporter)


@pytest.mark.parametrize("consume_first", [False, True])
@pytest.mark.asyncio
async def test_real_async_provider_stream_early_close(capture, consume_first):
    body = AsyncBytes()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )
    )
    async with openai.AsyncOpenAI(
        api_key="synthetic-key", http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        with capture() as exporter:
            stream = await litellm.acompletion(
                model="gpt-4o-mini", messages=MESSAGES, stream=True, client=client
            )
            if consume_first:
                await stream.__anext__()
            await stream.aclose()
            await stream.aclose()
            assert body.close_count == 1
            attributes(exporter)


@pytest.mark.asyncio
async def test_real_async_embedding_positional_input(capture):
    response = {
        "object": "list",
        "model": "text-embedding-ada-002",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.12345, 0.56789]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    async with openai.AsyncOpenAI(
        api_key="synthetic-key", http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        with capture() as exporter:
            result = await litellm.aembedding(
                "text-embedding-ada-002", ["synthetic-embedding-input"], client=client
            )
            assert result.data
        attrs = attributes(exporter)
        assert "synthetic-embedding-input" in attrs[SpanAttributes.INPUT_VALUE]
        assert (
            "synthetic-embedding-input"
            in attrs["embedding.embeddings.0.embedding.text"]
        )
        assert attrs[SpanAttributes.GEN_AI_SPAN_KIND] == "EMBEDDING"
        assert attrs[SpanAttributes.EMBEDDING_MODEL_NAME] == "text-embedding-ada-002"
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 1


def test_real_complete_response_type_preserved(capture):
    kwargs = dict(
        model="gpt-4o-mini",
        messages=MESSAGES,
        stream=True,
        complete_response=True,
        mock_response="hello",
        api_key="synthetic-key",
    )
    baseline = litellm.completion(**kwargs)
    with capture() as exporter:
        result = litellm.completion(**kwargs)
        assert type(result) is type(baseline)
        assert result.choices[0].message.content == baseline.choices[0].message.content
        assert attributes(exporter)[SpanAttributes.OUTPUT_VALUE] == "hello"


@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("from_environment", [False, True])
def test_embedding_vectors_only_use_privacy_filtered_attributes(
    capture, monkeypatch, hide, from_environment
):
    data = [
        {"object": "embedding", "index": 0, "embedding": [0.12345, 0.56789]},
        {"object": "embedding", "index": 1, "embedding": [0.98765, 0.43210]},
    ]
    response = EmbeddingResponse(
        model="text-embedding-ada-002",
        data=copy.deepcopy(data),
        usage={"prompt_tokens": 1, "total_tokens": 1},
    )
    monkeypatch.setattr(litellm, "embedding", lambda **kwargs: response)
    if from_environment:
        monkeypatch.setenv("FI_HIDE_EMBEDDING_VECTORS", str(hide).lower())
        config = TraceConfig()
    else:
        config = TraceConfig(hide_embedding_vectors=hide)
    with capture(config) as exporter:
        result = litellm.embedding(model="text-embedding-ada-002", input=["one", "two"])
    assert result is response
    assert result.data == data
    attrs = attributes(exporter)
    assert SpanAttributes.OUTPUT_VALUE not in attrs
    assert SpanAttributes.EMBEDDING_EMBEDDINGS not in attrs
    for index, item in enumerate(data):
        key = f"embedding.embeddings.{index}.embedding.vector"
        if hide:
            assert key not in attrs
            for value in item["embedding"]:
                assert str(value) not in json.dumps(attrs)
        else:
            assert json.loads(attrs[key]) == item["embedding"]
    assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 1


def tool_call(suffix, arguments):
    return {
        "id": f"call_synthetic_{suffix}",
        "type": "function",
        "function": {"name": f"lookup_{suffix}", "arguments": arguments},
    }


def test_tool_calls_preserved(capture, monkeypatch):
    response = ModelResponse(
        model="gpt-4o-mini",
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": "Calling a tool",
                    "tool_calls": [tool_call("out", '{"x":2}')],
                }
            }
        ],
    )
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response)
    messages = MESSAGES + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [tool_call("in", '{"x":1}')],
        },
        {"role": "tool", "tool_call_id": "call_synthetic_in", "content": "tool result"},
    ]
    original_messages = copy.deepcopy(messages)
    with capture() as exporter:
        assert litellm.completion(model="gpt-4o-mini", messages=messages) is response
    assert messages == original_messages
    attrs = attributes(exporter)
    assert json.loads(attrs[SpanAttributes.INPUT_VALUE])["messages"] == messages
    output = json.loads(attrs[SpanAttributes.OUTPUT_VALUE])["choices"][0]["message"]
    assert output["content"] == "Calling a tool"
    assert output["tool_calls"][0]["function"]["name"] == "lookup_out"
    for direction, index, suffix, arguments in (
        ("input", 1, "in", '{"x":1}'),
        ("output", 0, "out", '{"x":2}'),
    ):
        prefix = f"gen_ai.{direction}.messages.{index}.message.tool_calls.0"
        assert attrs[f"{prefix}.tool_call.id"] == f"call_synthetic_{suffix}"
        assert attrs[f"{prefix}.tool_call.function.name"] == f"lookup_{suffix}"
        assert attrs[f"{prefix}.tool_call.function.arguments"] == arguments
    assert attrs["gen_ai.input.messages.2.message.tool_call_id"] == "call_synthetic_in"


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_stream_mixed_text_and_tools_preserved(capture, monkeypatch, async_call):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": "Calling a tool",
                        "tool_calls": [tool_call("out", '{"x":')],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": "2}"}}]
                    }
                }
            ]
        },
    ]

    def completion(**kwargs):
        return iter(chunks)

    async def async_chunks():
        for chunk in chunks:
            yield chunk

    async def acompletion(**kwargs):
        return async_chunks()

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    with capture() as exporter:
        if async_call:
            stream = await litellm.acompletion(
                model="gpt-4o-mini", messages=MESSAGES, stream=True
            )
            assert [chunk async for chunk in stream] == chunks
        else:
            assert (
                list(
                    litellm.completion(
                        model="gpt-4o-mini", messages=MESSAGES, stream=True
                    )
                )
                == chunks
            )
    assert json.loads(attributes(exporter)[SpanAttributes.OUTPUT_VALUE]) == chunks
