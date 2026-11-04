"""Legacy-route regressions using the real OpenAI SDK and offline HTTP transports."""

import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_fireworks import FireworksInstrumentor

BASE_URL = "https://api.fireworks.ai/inference/v1"
MODEL = "accounts/fireworks/models/synthetic"
TOKEN = "SYNTHETIC-AUTH-DO-NOT-EXPORT"
API_KEY = "SYNTHETIC-API-KEY-DO-NOT-EXPORT"
QUERY_KEY = "SYNTHETIC-QUERY-DO-NOT-EXPORT"
BODY_KEY = "SYNTHETIC-EXTRA-BODY-DO-NOT-EXPORT"
PARAMS = {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}]}
CHUNK = {
    "id": "chat-1", "object": "chat.completion.chunk", "created": 1,
    "model": MODEL,
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"},
                 "finish_reason": None}],
}
USAGE = {
    "id": "chat-1", "object": "chat.completion.chunk", "created": 1,
    "model": MODEL, "choices": [],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


@pytest.fixture
def traced():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FireworksInstrumentor()
    # Older mock-based tests leave the BaseInstrumentor singleton enabled.
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    with pytest.warns(DeprecationWarning):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


def response(request):
    # Credentials still reach the SDK transport; only telemetry must omit them.
    assert request.headers["Authorization"] == "Bearer " + TOKEN
    assert request.headers["X-API-Key"] == API_KEY
    assert request.url.params["api_key"] == QUERY_KEY
    assert json.loads(request.content)["credential"] == BODY_KEY
    return httpx.Response(200, json={
        "id": "chat-1", "object": "chat.completion", "created": 1, "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"},
                     "finish_reason": "stop"}], "usage": USAGE["usage"],
    })


def request_options():
    return {
        **PARAMS, "temperature": 0.5, "max_tokens": 10,
        "extra_headers": {"Authorization": "Bearer " + TOKEN, "X-API-Key": API_KEY},
        "extra_query": {"api_key": QUERY_KEY}, "extra_body": {"credential": BODY_KEY},
    }


def assert_safe_input(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    exported = str((span.attributes, span.events, span.status.description))
    for secret in (TOKEN, API_KEY, QUERY_KEY, BODY_KEY):
        assert secret not in exported
    raw = json.loads(span.attributes["input.value"])
    assert raw["messages"] == PARAMS["messages"]
    assert raw["temperature"] == 0.5
    assert raw["max_tokens"] == 10
    assert not {"extra_headers", "extra_query", "extra_body"} & raw.keys()


def test_sync_request_credentials_not_exported(traced):
    with openai.OpenAI(api_key="synthetic-client-key", base_url=BASE_URL, max_retries=0,
                       http_client=httpx.Client(transport=httpx.MockTransport(response))) as client:
        assert client.chat.completions.create(**request_options()).choices[0].message.content == "Hi"
    assert_safe_input(traced)


@pytest.mark.asyncio
async def test_async_request_credentials_not_exported(traced):
    async with openai.AsyncOpenAI(
        api_key="synthetic-client-key", base_url=BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    ) as client:
        assert (await client.chat.completions.create(**request_options())).choices[0].message.content == "Hi"
    assert_safe_input(traced)


def event(chunk):
    return ("data: " + json.dumps(chunk) + "\n\n").encode()


class SyncBody(httpx.SyncByteStream):
    def __init__(self, error=None):
        self.error = error
        self.close_count = 0

    def __iter__(self):
        yield event(CHUNK)
        if self.error:
            raise self.error
        yield event(USAGE)
        yield b"data: [DONE]\n\n"

    def close(self):
        self.close_count += 1


class AsyncBody(httpx.AsyncByteStream):
    def __init__(self, error=None, started=None):
        self.error = error
        self.started = started
        self.close_count = 0

    async def __aiter__(self):
        yield event(CHUNK)
        if self.started:
            self.started.set()
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        yield event(USAGE)
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.close_count += 1


def stream_transport(body):
    return httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=body,
    ))


def assert_finished(exporter, body, error=False, complete=False):
    assert body.close_count == 1
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == (StatusCode.ERROR if error else StatusCode.OK)
    if complete:
        assert spans[0].attributes["gen_ai.usage.total_tokens"] == 4
        chunks = json.loads(spans[0].attributes["output.value"])
        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hi"
        assert chunks[1]["usage"]["total_tokens"] == 4
    return spans[0]


@pytest.mark.parametrize("mode", ["unopened", "partial", "exhausted", "context", "context_early", "context_error"])
def test_sync_stream_lifecycle(traced, mode):
    body = SyncBody()
    error = ValueError("synthetic consumer error")
    with openai.OpenAI(api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                       http_client=httpx.Client(transport=stream_transport(body))) as client:
        stream = client.chat.completions.create(**PARAMS, stream=True)
        if mode == "unopened":
            stream.close()
        elif mode == "partial":
            assert next(stream).choices[0].delta.content == "Hi"
            stream.close()
        elif mode == "exhausted":
            assert len(list(stream)) == 2
        elif mode == "context_error":
            with pytest.raises(ValueError) as caught:
                with stream as entered:
                    assert entered is stream
                    raise error
            assert caught.value is error
        else:
            with stream as entered:
                assert entered is stream
                if mode == "context":
                    assert len(list(entered)) == 2
        stream.close()
        assert isinstance(stream, openai.Stream)
        assert stream.response.is_closed
        with pytest.raises(StopIteration):
            next(stream)
    assert_finished(traced, body, error=mode == "context_error", complete=mode in ("exhausted", "context"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unopened", "aclose", "partial", "exhausted", "context", "context_early", "context_error"])
async def test_async_stream_lifecycle(traced, mode):
    body = AsyncBody()
    error = ValueError("synthetic consumer error")
    async with openai.AsyncOpenAI(
        api_key=TOKEN, base_url=BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=stream_transport(body)),
    ) as client:
        stream = await client.chat.completions.create(**PARAMS, stream=True)
        if mode == "unopened":
            await stream.close()
        elif mode == "aclose":
            await stream.aclose()
        elif mode == "partial":
            assert (await stream.__anext__()).choices[0].delta.content == "Hi"
            await stream.close()
        elif mode == "exhausted":
            assert len([chunk async for chunk in stream]) == 2
        elif mode == "context_error":
            with pytest.raises(ValueError) as caught:
                async with stream as entered:
                    assert entered is stream
                    raise error
            assert caught.value is error
        else:
            async with stream as entered:
                assert entered is stream
                if mode == "context":
                    assert len([chunk async for chunk in entered]) == 2
        await stream.close()
        assert isinstance(stream, openai.AsyncStream)
        assert stream.response.is_closed
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
    assert_finished(traced, body, error=mode == "context_error", complete=mode in ("exhausted", "context"))


def test_sync_stream_read_error_closes_and_finishes(traced):
    error = httpx.ReadError("synthetic interrupted stream")
    body = SyncBody(error=error)
    with openai.OpenAI(api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                       http_client=httpx.Client(transport=stream_transport(body))) as client:
        stream = client.chat.completions.create(**PARAMS, stream=True)
        with pytest.raises((httpx.ReadError, openai.APIConnectionError)) as caught:
            list(stream)
        # Newer SDKs translate transport failures; preserve that exception chain.
        assert caught.value is error or caught.value.__cause__ is error
        assert_finished(traced, body, error=True)
        stream.close()
    assert_finished(traced, body, error=True)


@pytest.mark.asyncio
async def test_async_stream_read_error_closes_and_finishes(traced):
    error = httpx.ReadError("synthetic interrupted stream")
    body = AsyncBody(error=error)
    async with openai.AsyncOpenAI(
        api_key=TOKEN, base_url=BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=stream_transport(body)),
    ) as client:
        stream = await client.chat.completions.create(**PARAMS, stream=True)
        with pytest.raises((httpx.ReadError, openai.APIConnectionError)) as caught:
            _ = [chunk async for chunk in stream]
        # Newer SDKs translate transport failures; preserve that exception chain.
        assert caught.value is error or caught.value.__cause__ is error
        assert_finished(traced, body, error=True)
        await stream.close()
    assert_finished(traced, body, error=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cancellation_before_response_finishes_span(traced, stream):
    started = asyncio.Event()

    async def wait(request):
        started.set()
        await asyncio.Event().wait()

    async with openai.AsyncOpenAI(
        api_key=TOKEN, base_url=BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wait)),
    ) as client:
        task = asyncio.create_task(client.chat.completions.create(**PARAMS, stream=stream))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel("synthetic cancellation")
        with pytest.raises(asyncio.CancelledError):
            await task
    spans = traced.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].attributes["exception.type"] == "asyncio.exceptions.CancelledError"


@pytest.mark.asyncio
async def test_cancellation_during_stream_closes_and_finishes(traced):
    started = asyncio.Event()
    body = AsyncBody(started=started)
    async with openai.AsyncOpenAI(
        api_key=TOKEN, base_url=BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=stream_transport(body)),
    ) as client:
        stream = await client.chat.completions.create(**PARAMS, stream=True)

        async def consume():
            return [chunk async for chunk in stream]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel("synthetic cancellation")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_finished(traced, body, error=True)
        await stream.close()
    assert_finished(traced, body, error=True)
