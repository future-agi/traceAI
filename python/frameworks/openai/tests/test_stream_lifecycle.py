"""Explicit close and task cancellation through the SDK's native HTTP transport."""

import asyncio
from contextlib import aclosing
import inspect
import json
import os
import sys

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_openai import OpenAIInstrumentor

# OpenAI 3 uses httpx2; do not exercise it through the legacy httpx adapter.
if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as http
else:
    import httpx as http

ARGS = {"model": "requested-model", "messages": [{"role": "user", "content": "hello"}]}
CHUNK = {
    "id": "test", "object": "chat.completion.chunk", "created": 1, "model": "served-model",
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "partial-answer"},
                 "finish_reason": None}],
}
CHUNK_BYTES = ("data: " + json.dumps(CHUNK) + "\n\n").encode()


@pytest.fixture(params=[
    "https://model-synthetic.api.baseten.co/environments/production/sync/v1",
    "https://model-synthetic-us-west.api.baseten.co/sync/v1",
])
def base_url(request):
    return request.param


@pytest.fixture
def tracing(monkeypatch, request):
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_")):
            monkeypatch.delenv(name)
    config = TraceConfig()
    if getattr(request, "param", False):
        if "hide_error_details" not in inspect.signature(TraceConfig).parameters:
            pytest.skip("The locked legacy core predates error-detail masking")
        config = TraceConfig(hide_error_details=True)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    yield provider, exporter
    instrumentor.uninstrument()
    provider.shutdown()


def client(handler, base_url, use_async=False):
    sdk_type = openai.AsyncOpenAI if use_async else openai.OpenAI
    http_type = http.AsyncClient if use_async else http.Client
    return sdk_type(
        api_key="synthetic-key", base_url=base_url, max_retries=0,
        http_client=http_type(transport=http.MockTransport(handler), trust_env=False),
    )


def streaming(_):
    return http.Response(
        200, headers={"content-type": "text/event-stream"},
        content=CHUNK_BYTES + b"data: [DONE]\n\n",
    )


def cancelled(message):
    # Task.cancel(msg) propagates msg to the awaiter only on Python 3.11+.
    return pytest.raises(
        asyncio.CancelledError, match=message if sys.version_info >= (3, 11) else None,
    )


def only_span(tracing, status):
    provider, exporter = tracing
    assert provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == status
    assert span.attributes["gen_ai.provider.name"] == "baseten"
    return span


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("consumption", ["empty", "partial", "exhausted"])
def test_explicit_close_finishes_once(tracing, base_url, use_async, consumption):
    if use_async:
        async def run():
            async with client(streaming, base_url, True) as sdk:
                stream = await sdk.chat.completions.create(**ARGS, stream=True)
                if consumption == "partial":
                    await stream.__anext__()
                elif consumption == "exhausted":
                    async for _ in stream:
                        pass
                assert await stream.close() is None
                assert stream.response.is_closed
                only_span(tracing, StatusCode.OK)
                # Repeated close and context-manager cleanup must not end twice.
                await stream.close()
                await stream.__aexit__(None, None, None)
        asyncio.run(run())
    else:
        with client(streaming, base_url) as sdk:
            stream = sdk.chat.completions.create(**ARGS, stream=True)
            if consumption == "partial":
                next(stream)
            elif consumption == "exhausted":
                list(stream)
            assert stream.close() is None
            assert stream.response.is_closed
            only_span(tracing, StatusCode.OK)
            stream.close()
            stream.__exit__(None, None, None)
    span = only_span(tracing, StatusCode.OK)
    if consumption != "empty":
        assert span.attributes["output.value"] == "partial-answer"
        assert span.attributes["gen_ai.request.model"] == ARGS["model"]


@pytest.mark.skipif(
    not hasattr(openai.AsyncStream, "aclose"),
    reason="This SDK version does not expose AsyncStream.aclose",
)
@pytest.mark.parametrize("consumption", ["empty", "partial", "exhausted"])
@pytest.mark.parametrize("cleanup", ["aclose", "stdlib_aclosing"])
def test_async_close_alias_finishes_once(tracing, base_url, consumption, cleanup):
    async def run():
        async with client(streaming, base_url, True) as sdk:
            stream = await sdk.chat.completions.create(**ARGS, stream=True)

            async def consume():
                if consumption == "partial":
                    await stream.__anext__()
                elif consumption == "exhausted":
                    async for _ in stream:
                        pass

            if cleanup == "stdlib_aclosing":
                async with aclosing(stream):
                    await consume()
            else:
                await consume()
                assert await stream.aclose() is None
            assert stream.response.is_closed
            only_span(tracing, StatusCode.OK)
            # Interleaved aliases and context cleanup must not export duplicates.
            await stream.aclose()
            await stream.close()
            await stream.__aexit__(None, None, None)

    asyncio.run(run())
    span = only_span(tracing, StatusCode.OK)
    if consumption != "empty":
        assert span.attributes["output.value"] == "partial-answer"
        assert span.attributes["gen_ai.request.model"] == ARGS["model"]


@pytest.mark.parametrize("use_async,close_method", [
    (False, "close"), (True, "close"),
    pytest.param(True, "aclose", marks=pytest.mark.skipif(
        not hasattr(openai.AsyncStream, "aclose"),
        reason="This SDK version does not expose AsyncStream.aclose",
    )),
])
def test_close_failure_preserves_exception_and_finishes(
    tracing, base_url, use_async, close_method, monkeypatch,
):
    error = RuntimeError("synthetic close failure")
    calls = []

    if use_async:
        async def fail_close():
            calls.append(True)
            raise error

        async def run():
            async with client(streaming, base_url, True) as sdk:
                stream = await sdk.chat.completions.create(**ARGS, stream=True)
                await stream.__anext__()
                with monkeypatch.context() as patch:
                    patch.setattr(stream.response, "aclose", fail_close)
                    with pytest.raises(RuntimeError) as caught:
                        await getattr(stream, close_method)()
                    assert caught.value is error
                    only_span(tracing, StatusCode.ERROR)
                await stream.close()
        asyncio.run(run())
    else:
        def fail_close():
            calls.append(True)
            raise error

        with client(streaming, base_url) as sdk:
            stream = sdk.chat.completions.create(**ARGS, stream=True)
            next(stream)
            with monkeypatch.context() as patch:
                patch.setattr(stream.response, "close", fail_close)
                with pytest.raises(RuntimeError) as caught:
                    stream.close()
                assert caught.value is error
            stream.close()
    assert calls == [True]
    span = only_span(tracing, StatusCode.ERROR)
    assert sum(event.name == "exception" for event in span.events) == 1


@pytest.mark.parametrize("tracing,hide_errors", [(False, False), (True, True)], indirect=["tracing"])
def test_cancel_pending_request_finishes(tracing, base_url, hide_errors):
    async def run():
        reached_transport = asyncio.Event()

        async def handler(_):
            reached_transport.set()
            await asyncio.Event().wait()

        async with client(handler, base_url, True) as sdk:
            task = asyncio.create_task(sdk.chat.completions.create(**ARGS))
            await asyncio.wait_for(reached_transport.wait(), 2)
            task.cancel("synthetic request cancellation")
            with cancelled("synthetic request cancellation"):
                await task
            assert task.cancelled()
            # Finalization must not depend on subsequent client cleanup.
            only_span(tracing, StatusCode.ERROR)
    asyncio.run(run())
    span = only_span(tracing, StatusCode.ERROR)
    if hide_errors:
        assert "synthetic request cancellation" not in span.to_json()
    else:
        assert any(event.attributes["exception.type"] == "asyncio.exceptions.CancelledError"
                   for event in span.events if event.name == "exception")


@pytest.mark.parametrize("tracing,hide_errors", [(False, False), (True, True)], indirect=["tracing"])
def test_cancel_stream_iteration_finishes_before_close(tracing, base_url, hide_errors):
    async def run():
        waiting_for_chunk = asyncio.Event()
        closed = []

        class SlowBody(http.AsyncByteStream):
            async def __aiter__(self):
                yield CHUNK_BYTES
                waiting_for_chunk.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.append(True)

        def handler(_):
            return http.Response(
                200, headers={"content-type": "text/event-stream"}, stream=SlowBody(),
            )

        async with client(handler, base_url, True) as sdk:
            stream = await sdk.chat.completions.create(**ARGS, stream=True)
            await stream.__anext__()
            task = asyncio.create_task(stream.__anext__())
            await asyncio.wait_for(waiting_for_chunk.wait(), 2)
            task.cancel("synthetic stream cancellation")
            with cancelled("synthetic stream cancellation"):
                await task
            assert task.cancelled()
            only_span(tracing, StatusCode.ERROR)
            await stream.close()
            await stream.close()
            await stream.__aexit__(None, None, None)
            assert stream.response.is_closed
            assert closed == [True]
    asyncio.run(run())
    span = only_span(tracing, StatusCode.ERROR)
    assert span.attributes["output.value"] == "partial-answer"
    if hide_errors:
        assert "synthetic stream cancellation" not in span.to_json()
    else:
        assert sum(event.name == "exception" for event in span.events) == 1


@pytest.mark.parametrize("close_method", [
    "close",
    pytest.param("aclose", marks=pytest.mark.skipif(
        not hasattr(openai.AsyncStream, "aclose"),
        reason="This SDK version does not expose AsyncStream.aclose",
    )),
])
def test_cancel_async_close_finishes(tracing, base_url, close_method):
    async def run():
        closing = asyncio.Event()

        class SlowCloseBody(http.AsyncByteStream):
            async def __aiter__(self):
                yield CHUNK_BYTES

            async def aclose(self):
                closing.set()
                await asyncio.Event().wait()

        def handler(_):
            return http.Response(
                200, headers={"content-type": "text/event-stream"}, stream=SlowCloseBody(),
            )

        async with client(handler, base_url, True) as sdk:
            stream = await sdk.chat.completions.create(**ARGS, stream=True)
            await stream.__anext__()
            task = asyncio.create_task(getattr(stream, close_method)())
            await asyncio.wait_for(closing.wait(), 2)
            task.cancel("synthetic close cancellation")
            with cancelled("synthetic close cancellation"):
                await task
            assert task.cancelled()
            only_span(tracing, StatusCode.ERROR)
    asyncio.run(run())
    only_span(tracing, StatusCode.ERROR)
