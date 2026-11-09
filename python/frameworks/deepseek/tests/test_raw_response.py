"""Real SDK raw-response contracts, using only synthetic headers and MockTransport."""
import asyncio
import inspect
import json
from contextlib import AsyncExitStack

import openai
import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_deepseek import DeepSeekInstrumentor

try:
    from openai._base_client import httpx2 as httpx
except ImportError:
    from openai._base_client import httpx

PROMPT = "PRIVATE_RAW_PROMPT"
SECRET = "SYNTHETIC_AUTHORIZATION_SECRET"
BODY = {
    "id": "raw-completion", "created": 1, "object": "chat.completion",
    "model": "deepseek-chat",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}
CHUNKS = [
    {**BODY, "object": "chat.completion.chunk", "usage": None,
     "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]},
    {**BODY, "object": "chat.completion.chunk",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
]


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    DeepSeekInstrumentor().uninstrument()
    provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
async def test_raw_privacy_usage_and_cached_parse(telemetry, asynchronous, streaming, hidden, lazy):
    provider, exporter = telemetry
    DeepSeekInstrumentor().instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=hidden))
    requests = []

    def respond(request):
        assert request.headers["authorization"] == "Bearer " + SECRET
        requests.append(json.loads(request.content))
        headers = {"x-request-id": "synthetic-request-id"}
        if streaming:
            headers["content-type"] = "text/event-stream"
            data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in CHUNKS)
            return httpx.Response(200, content=data + "data: [DONE]\n\n", headers=headers)
        return httpx.Response(200, json=BODY, headers=headers)

    client_type = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    transport_type = httpx.AsyncClient if asynchronous else httpx.Client
    client = client_type(api_key="synthetic-client-key", base_url="https://api.deepseek.com/v1",
                         http_client=transport_type(transport=httpx.MockTransport(respond)))
    async with AsyncExitStack() as stack:
        if asynchronous:
            await stack.enter_async_context(client)
        else:
            stack.enter_context(client)
        completions = client.chat.completions
        if lazy and not hasattr(completions, "with_streaming_response"):
            pytest.skip("SDK 1.0 has with_raw_response only")
        resource = completions.with_streaming_response if lazy else completions.with_raw_response
        raw = await resolve(resource.create(
            model="deepseek-chat", messages=[{"role": "user", "content": PROMPT}], stream=streaming,
            extra_headers={"Authorization": "Bearer " + SECRET},
        ))
        if lazy:
            raw = await stack.enter_async_context(raw) if asynchronous else stack.enter_context(raw)
        assert raw.status_code == 200
        assert raw.headers["x-request-id"] == "synthetic-request-id"
        if not lazy and not streaming:
            # Buffered raw responses must not leak a span for header-only users.
            assert len(exporter.get_finished_spans()) == 1
        parsed = await resolve(raw.parse())
        assert await resolve(raw.parse()) is parsed
        if streaming:
            assert not exporter.get_finished_spans()
            values = [chunk async for chunk in parsed] if asynchronous else list(parsed)
            assert len(values) == 2
        else:
            assert parsed.choices[0].message.content == "answer"
        assert await resolve(raw.parse()) is parsed
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["gen_ai.usage.input_tokens"] == 11
        serialized = spans[0].to_json()
        assert SECRET not in serialized
        assert "synthetic-client-key" not in serialized
        assert "extra_headers" not in serialized
        assert (PROMPT in serialized) is not hidden
        assert spans[0].status.status_code == StatusCode.OK
        assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("parsed", ["unparsed", "unconsumed", "partial"])
async def test_raw_close_ends_span_without_consuming_stream(telemetry, asynchronous, parsed):
    provider, exporter = telemetry
    DeepSeekInstrumentor().instrument(tracer_provider=provider)

    def respond(request):
        return httpx.Response(200, content="data: " + json.dumps(CHUNKS[0]) + "\n\n",
                              headers={"content-type": "text/event-stream"})

    client_type = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    transport_type = httpx.AsyncClient if asynchronous else httpx.Client
    client = client_type(api_key="synthetic", base_url="https://api.deepseek.com/v1",
                         http_client=transport_type(transport=httpx.MockTransport(respond)))
    try:
        raw = await resolve(client.chat.completions.with_raw_response.create(
            model="deepseek-chat", messages=[], stream=True))
        if parsed != "unparsed":
            stream = await resolve(raw.parse())
            assert await resolve(raw.parse()) is stream
            if parsed == "partial":
                await stream.__anext__() if asynchronous else next(stream)
        assert not exporter.get_finished_spans()
        await resolve(raw.close())
        await resolve(raw.close())
        assert raw.http_response.is_closed
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET
        if parsed == "partial":
            assert json.loads(spans[0].attributes["output.value"])["content"] == "answer"
    finally:
        await resolve(client.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_raw_parse_and_iteration_errors_end_once(telemetry, asynchronous, streaming):
    provider, exporter = telemetry
    DeepSeekInstrumentor().instrument(tracer_provider=provider)

    def respond(request):
        data = ("data: " + json.dumps(CHUNKS[0]) + "\n\ndata: not-json\n\n") if streaming else "not-json"
        return httpx.Response(200, content=data, headers={
            "content-type": "text/event-stream" if streaming else "application/json"})

    client_type = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    transport_type = httpx.AsyncClient if asynchronous else httpx.Client
    client = client_type(api_key="synthetic", base_url="https://api.deepseek.com/v1",
                         http_client=transport_type(transport=httpx.MockTransport(respond)))
    try:
        raw = await resolve(client.chat.completions.with_raw_response.create(
            model="deepseek-chat", messages=[], stream=streaming))
        with pytest.raises(json.JSONDecodeError):
            parsed = await resolve(raw.parse())
            if streaming:
                if asynchronous:
                    _ = [chunk async for chunk in parsed]
                else:
                    list(parsed)
        await resolve(raw.close())
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
    finally:
        await resolve(client.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("phase", ["construction", "iteration", "parse"])
async def test_raw_lifecycle_preserves_exception_identity(telemetry, asynchronous, phase):
    """Failure/cancellation after HTTP success must not strand the model span."""
    from types import SimpleNamespace
    from traceai_deepseek._wrappers import _AsyncChatCompletionWrapper, _ChatCompletionWrapper

    provider, exporter = telemetry
    error = asyncio.CancelledError("synthetic cancellation") if asynchronous else ValueError("synthetic failure")

    class BrokenStream:
        def __iter__(self):
            if phase == "construction":
                raise error
            return self

        def __next__(self):
            raise error

        def __aiter__(self):
            if phase == "construction":
                raise error
            return self

        async def __anext__(self):
            raise error

    class RawResponse:
        def parse(self):
            if phase == "parse":
                raise error
            return BrokenStream()

    raw = RawResponse()
    async def create_async(**kwargs):
        return raw

    wrapper_type = _AsyncChatCompletionWrapper if asynchronous else _ChatCompletionWrapper
    wrapper = wrapper_type(FITracer(provider.get_tracer(__name__), config=TraceConfig()))
    create = create_async if asynchronous else lambda **kwargs: raw
    instance = SimpleNamespace(_client=SimpleNamespace(base_url="https://api.deepseek.com"))
    with pytest.raises(type(error)) as caught:
        result = await resolve(wrapper(create, instance, (), {"model": "deepseek-chat", "stream": True}))
        stream = await resolve(result.parse())
        if asynchronous:
            await stream.__anext__()
        else:
            next(stream)
    assert caught.value is error
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_unknown_response_objects_are_never_serialized():
    from traceai_deepseek._utils import _to_dict

    class RequestOptions:
        def __init__(self):
            self.authorization = SECRET
            self.messages = PROMPT

        def __str__(self):
            return SECRET

    assert _to_dict(RequestOptions()) == {}
