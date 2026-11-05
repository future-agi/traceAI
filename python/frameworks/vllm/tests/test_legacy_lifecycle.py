"""Legacy span lifecycle and privacy with the real OpenAI SDK and no network."""

import asyncio
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_vllm import VLLMInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx

OPTIONS = {
    "base_url": "http://localhost:8000/v1",
    "api_key": "synthetic-not-a-real-key",
    "max_retries": 0,
}
PARAMS = {
    "model": "test-vllm-model",
    "messages": [{"role": "user", "content": "Hello"}],
}
COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "test-vllm-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Synthetic output"},
        }
    ],
}


def _chunk(content):
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-vllm-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }


def _sse(parts):
    body = "".join(f"data: {json.dumps(_chunk(part))}\n\n" for part in parts)
    return body + "data: [DONE]\n\n"


def _stream_response(request):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse(["part0", "part1", "part2"]),
    )


class _Abort(BaseException):
    """A non-Exception interruption, like KeyboardInterrupt or SystemExit."""


@pytest.fixture
def exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = VLLMInstrumentor()
    with pytest.warns(DeprecationWarning):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def _sync_client(handler):
    return openai.OpenAI(
        **OPTIONS,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )


def _async_client(handler):
    return openai.AsyncOpenAI(
        **OPTIONS,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )


def _only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [(s.name, s.status.status_code) for s in spans]
    return spans[0]


def _assert_error(span, exception_type):
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"].split(".")[-1] == exception_type
    recorded = [
        e.attributes["exception.type"] for e in span.events if e.name == "exception"
    ]
    assert [name.split(".")[-1] for name in recorded] == [exception_type]


def _assert_streamed_output(span):
    assert span.status.status_code == StatusCode.OK
    assert (
        span.attributes["gen_ai.output.messages.0.message.content"] == "part0part1part2"
    )


@pytest.mark.parametrize("use_async", [False, True])
def test_transport_options_are_not_exported(exporter, use_async):
    seen = {}

    def handle(request):
        seen["authorization"] = request.headers.get("authorization")
        seen["query"] = request.url.params.get("key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETION)

    request = {
        **PARAMS,
        "extra_headers": {"Authorization": "Bearer SYNTHETIC-HEADER-TOKEN"},
        "extra_query": {"key": "SYNTHETIC-QUERY-TOKEN"},
        "extra_body": {"top_k": 5},
        "timeout": 5.0,
    }

    async def run_async():
        async with _async_client(handle) as client:
            await client.chat.completions.create(**request)

    if use_async:
        asyncio.run(run_async())
    else:
        with _sync_client(handle) as client:
            client.chat.completions.create(**request)

    # The SDK still sends every option; only the exported span omits them.
    assert seen["authorization"] == "Bearer SYNTHETIC-HEADER-TOKEN"
    assert seen["query"] == "SYNTHETIC-QUERY-TOKEN"
    assert seen["body"]["top_k"] == 5
    span = _only_span(exporter)
    exported = json.dumps(dict(span.attributes))
    assert "SYNTHETIC-HEADER-TOKEN" not in exported
    assert "SYNTHETIC-QUERY-TOKEN" not in exported
    recorded = json.loads(span.attributes["input.value"])
    assert set(recorded) == {"model", "messages", "extra_body"}
    # Request-body parameters such as vLLM sampling options remain visible.
    assert recorded["extra_body"] == {"top_k": 5}


def test_async_call_cancelled_by_timeout_is_exported_as_error(exporter):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=COMPLETION)

    async def run():
        async with _async_client(slow) as client:
            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(client.chat.completions.create(**PARAMS), 0.2)

    asyncio.run(run())
    _assert_error(_only_span(exporter), "CancelledError")


def test_sync_call_interrupted_is_exported_as_error(exporter):
    def interrupt(request):
        raise _Abort("synthetic interrupt")

    with _sync_client(interrupt) as client:
        with pytest.raises(_Abort):
            client.chat.completions.create(**PARAMS)
    _assert_error(_only_span(exporter), "_Abort")


def test_async_stream_cancelled_midway_is_exported_as_error(exporter):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {json.dumps(_chunk('first'))}\n\n".encode()
            await asyncio.sleep(5)
            yield b"data: [DONE]\n\n"

    async def slow_stream(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SlowStream()
        )

    async def run():
        async with _async_client(slow_stream) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)

            async def consume():
                async for _ in stream:
                    pass

            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(consume(), 0.3)

    asyncio.run(run())
    _assert_error(_only_span(exporter), "CancelledError")


def test_sync_stream_failure_midway_is_exported_as_error(exporter):
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield f"data: {json.dumps(_chunk('first'))}\n\n".encode()
            raise httpx.ReadError("synthetic disconnect")

    def broken(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
        )

    with _sync_client(broken) as client:
        stream = client.chat.completions.create(**PARAMS, stream=True)
        # Newer SDKs wrap transport read errors in APIConnectionError.
        with pytest.raises((httpx.ReadError, openai.APIConnectionError)) as raised:
            for _ in stream:
                pass
    _assert_error(_only_span(exporter), type(raised.value).__name__)


def test_sync_stream_keeps_sdk_stream_api(exporter):
    with _sync_client(_stream_response) as client:
        stream = client.chat.completions.create(**PARAMS, stream=True)
        assert isinstance(stream, openai.Stream)
        assert stream.response.status_code == 200
        assert exporter.get_finished_spans() == ()
        assert [c.choices[0].delta.content for c in stream] == [
            "part0",
            "part1",
            "part2",
        ]
    _assert_streamed_output(_only_span(exporter))


@pytest.mark.skipif(
    not hasattr(openai.Stream, "__enter__"),
    reason="this SDK's Stream is not a context manager",
)
def test_sync_stream_context_manager(exporter):
    with _sync_client(_stream_response) as client:
        with client.chat.completions.create(**PARAMS, stream=True) as stream:
            assert isinstance(stream, openai.Stream)
            assert len(list(stream)) == 3
    _assert_streamed_output(_only_span(exporter))


@pytest.mark.skipif(
    not hasattr(openai.Stream, "__enter__"),
    reason="this SDK's Stream is not a context manager",
)
def test_sync_stream_context_manager_exited_early(exporter):
    with _sync_client(_stream_response) as client:
        with client.chat.completions.create(**PARAMS, stream=True) as stream:
            next(stream)
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["gen_ai.output.messages.0.message.content"] == "part0"


def test_sync_stream_closed_early(exporter):
    with _sync_client(_stream_response) as client:
        stream = client.chat.completions.create(**PARAMS, stream=True)
        for _ in stream:
            break
        stream.close()
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["gen_ai.output.messages.0.message.content"] == "part0"


def test_async_stream_keeps_sdk_stream_api(exporter):
    async def run():
        async with _async_client(_stream_response) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)
            assert isinstance(stream, openai.AsyncStream)
            assert stream.response.status_code == 200
            assert [c.choices[0].delta.content async for c in stream] == [
                "part0",
                "part1",
                "part2",
            ]

    asyncio.run(run())
    _assert_streamed_output(_only_span(exporter))


@pytest.mark.skipif(
    not hasattr(openai.AsyncStream, "__aenter__"),
    reason="this SDK's AsyncStream is not a context manager",
)
def test_async_stream_context_manager(exporter):
    async def run():
        async with _async_client(_stream_response) as client:
            async with await client.chat.completions.create(
                **PARAMS, stream=True
            ) as stream:
                assert isinstance(stream, openai.AsyncStream)
                assert len([c async for c in stream]) == 3

    asyncio.run(run())
    _assert_streamed_output(_only_span(exporter))


def test_async_stream_closed_early(exporter):
    async def run():
        async with _async_client(_stream_response) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)
            async for _ in stream:
                break
            await stream.close()

    asyncio.run(run())
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["gen_ai.output.messages.0.message.content"] == "part0"


def test_raw_response_stream_is_returned_unchanged(exporter):
    with _sync_client(_stream_response) as client:
        raw = client.chat.completions.with_raw_response.create(**PARAMS, stream=True)
        assert raw.http_response.status_code == 200
        assert len(list(raw.parse())) == 3
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.OK
    assert "synthetic-not-a-real-key" not in json.dumps(dict(span.attributes))


def test_raw_response_does_not_export_client_internals(exporter):
    def handle(request):
        return httpx.Response(200, json=COMPLETION)

    with _sync_client(handle) as client:
        raw = client.chat.completions.with_raw_response.create(
            **PARAMS, extra_headers={"Authorization": "Bearer SYNTHETIC-HEADER-TOKEN"}
        )
        assert raw.parse().choices[0].message.content == "Synthetic output"
    exported = json.dumps(dict(_only_span(exporter).attributes))
    assert "SYNTHETIC-HEADER-TOKEN" not in exported
    assert "synthetic-not-a-real-key" not in exported
