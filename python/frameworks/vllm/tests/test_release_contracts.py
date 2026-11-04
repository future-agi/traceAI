"""Real SDK regressions: transport privacy, native streams and cancellation."""

import asyncio
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_vllm import VLLMInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx

INPUT = "PRIVATE_INPUT_SENTINEL"
OUTPUT = "PRIVATE_OUTPUT_SENTINEL"
SECRET = "SYNTHETIC_TRANSPORT_SECRET"
PARAMS = {"model": "test", "messages": [{"role": "user", "content": INPUT}]}
OPTIONS = {"api_key": "synthetic-key", "base_url": "http://localhost:8000/v1", "max_retries": 0}


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inst = VLLMInstrumentor()

    def start(**privacy):
        with pytest.warns(DeprecationWarning):
            inst.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return exporter

    yield start
    inst.uninstrument()
    provider.shutdown()


def response(request):
    body = json.loads(request.content)
    data = {
        "id": "test", "object": "chat.completion", "created": 1, "model": "test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": OUTPUT}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    if body.get("stream"):
        data["object"] = "chat.completion.chunk"
        data["choices"][0]["delta"] = data["choices"][0].pop("message")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n")
    return httpx.Response(200, json=data)


@pytest.mark.parametrize("use_async", [False, True])
def test_transport_credentials_excluded_without_changing_request(tracing, use_async):
    exporter = tracing()
    seen = []

    def handle(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert json.loads(request.content)["transport_secret"] == SECRET
        assert request.url.params["secret"] == SECRET
        return response(request)

    params = {**PARAMS, "extra_headers": {"Authorization": "Bearer " + SECRET},
              "extra_query": {"secret": SECRET}, "extra_body": {"transport_secret": SECRET}}

    async def run():
        async with openai.AsyncOpenAI(**OPTIONS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            await client.chat.completions.create(**params)

    if use_async:
        asyncio.run(run())
    else:
        with openai.OpenAI(**OPTIONS, http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            client.chat.completions.create(**params)
    assert len(seen) == 1
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert SECRET not in spans[0].to_json()
    assert INPUT in spans[0].to_json()


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("mode", ["exhaust", "early", "unused", "close"])
def test_native_stream_lifecycle(tracing, use_async, mode):
    exporter = tracing()

    async def run():
        async with openai.AsyncOpenAI(**OPTIONS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(response))) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)
            assert isinstance(stream, openai.AsyncStream)
            if mode == "close":
                await stream.close()
            else:
                async with stream as entered:
                    assert entered is stream
                    if mode == "exhaust":
                        assert [chunk async for chunk in stream]
                    elif mode == "early":
                        assert (await stream.__anext__()).choices[0].delta.content == OUTPUT
            assert stream.response.is_closed
            await stream.close()

    if use_async:
        asyncio.run(run())
    else:
        with openai.OpenAI(**OPTIONS, http_client=httpx.Client(transport=httpx.MockTransport(response))) as client:
            stream = client.chat.completions.create(**PARAMS, stream=True)
            assert isinstance(stream, openai.Stream)
            if mode == "close":
                stream.close()
            else:
                with stream as entered:
                    assert entered is stream
                    if mode == "exhaust":
                        assert list(stream)
                    elif mode == "early":
                        assert next(stream).choices[0].delta.content == OUTPUT
            assert stream.response.is_closed
            stream.close()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.is_ok


@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("phase", ["request", "stream"])
def test_cancel_preserves_exception_and_finishes_once(tracing, hide, phase):
    exporter = tracing(hide_error_details=hide)

    async def run():
        entered = asyncio.Event()

        class BlockedBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

        async def handle(request):
            if phase == "request":
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BlockedBody())

        async with openai.AsyncOpenAI(**OPTIONS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            stream = None
            if phase == "stream":
                stream = await client.chat.completions.create(**PARAMS, stream=True)
                task = asyncio.create_task(stream.__anext__())
            else:
                task = asyncio.create_task(client.chat.completions.create(**PARAMS))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel(SECRET)
            with pytest.raises(asyncio.CancelledError, match=SECRET):
                await task
            if stream is not None:
                await stream.close()

    asyncio.run(run())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"
    assert (SECRET not in spans[0].to_json()) == hide
