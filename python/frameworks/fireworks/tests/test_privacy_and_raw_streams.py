"""Privacy and ownership regressions against real, offline OpenAI clients."""

import asyncio
import copy
import gc
import inspect
import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_fireworks import FireworksInstrumentor
from traceai_fireworks._stream import _AsyncStream, _Stream

from .test_sdk_lifecycle import (
    BASE_URL, MODEL, PARAMS, TOKEN, AsyncBody, SyncBody, assert_finished,
    stream_transport, traced as traced,
)


@contextmanager
def configured_tracing(**config):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FireworksInstrumentor()
    with pytest.warns(DeprecationWarning):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**config))
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def sync_client(body):
    return openai.OpenAI(
        api_key=TOKEN, base_url=BASE_URL, max_retries=0,
        http_client=httpx.Client(transport=stream_transport(body)),
    )


def async_client(body):
    return openai.AsyncOpenAI(
        api_key=TOKEN, base_url=BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=stream_transport(body)),
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("env", [False, True])
@pytest.mark.parametrize("policy", ["hide", "limit", "boundary", "visible"])
@pytest.mark.parametrize("image_kind", ["base64", "remote"])
def test_image_privacy_every_exported_copy(monkeypatch, asynchronous, env, policy, image_kind):
    sentinel = "SYNTHETIC-PRIVATE-IMAGE"
    url = ("data:image/png;base64," if image_kind == "base64" else "https://example.invalid/") + sentinel
    params = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
    ]}]}
    original = copy.deepcopy(params)
    config = {}
    if policy == "hide":
        config["hide_input_images"] = True
    elif policy in ("limit", "boundary"):
        config["base64_image_max_length"] = len(url) - (policy == "limit")
    if env:
        for key, value in config.items():
            monkeypatch.setenv("FI_" + key.upper(), str(value).lower())
        config = {}
    requests = []

    def response(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat-1", "object": "chat.completion", "created": 1, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"},
                         "finish_reason": "stop"}],
        })

    with configured_tracing(**config) as exporter:
        if asynchronous:
            async def call():
                async with openai.AsyncOpenAI(
                    api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
                ) as client:
                    await client.chat.completions.create(**params)
            asyncio.run(call())
        else:
            with openai.OpenAI(
                api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(response)),
            ) as client:
                client.chat.completions.create(**params)
        assert params == original
        assert requests[0]["messages"] == original["messages"]
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert "Describe this image" in attrs["input.value"]
        assert "Describe this image" in attrs["gen_ai.input.messages.0.message.content"]
        hidden = policy == "hide" or (policy == "limit" and image_kind == "base64")
        if hidden:
            assert not [key for key, value in attrs.items() if sentinel in str(value)]
        else:
            assert sentinel in attrs["input.value"]
            assert sentinel in attrs["gen_ai.input.messages.0.message.content"]


@pytest.mark.parametrize("consumed", [False, True])
def test_sync_abandonment_ends_without_claiming_success(traced, consumed):
    with sync_client(SyncBody()) as client:
        stream = client.chat.completions.create(**PARAMS, stream=True)
        if consumed:
            next(stream)
        del stream
        gc.collect()
        spans = traced.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_async_abandonment_ends_without_claiming_success(traced, consumed):
    async with async_client(AsyncBody()) as client:
        stream = await client.chat.completions.create(**PARAMS, stream=True)
        if consumed:
            await stream.__anext__()
        del stream
        gc.collect()
        for _ in range(5):
            await asyncio.sleep(0)
        spans = traced.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET


@pytest.mark.parametrize("proxy", [_Stream, _AsyncStream])
def test_finalizer_does_not_retain_proxy_or_call_async_cleanup(proxy):
    native, span = MagicMock(), MagicMock()
    stream = proxy(native, span)
    del stream
    gc.collect()
    span.end.assert_called_once_with()
    span.set_status.assert_not_called()
    native.close.assert_not_called()
    native.aclose.assert_not_called()


@pytest.mark.parametrize("mode", ["exhaust", "early", "raw_close", "unparsed", "error", "parse_error", "detach", "abandon"])
def test_sync_raw_stream_ownership(traced, mode, monkeypatch):
    error = httpx.ReadError("synthetic stream failure")
    body = SyncBody(error=error if mode == "error" else None)
    with sync_client(body) as client:
        raw = client.chat.completions.with_raw_response.create(**PARAMS, stream=True)
        assert raw.status_code == 200
        assert raw.headers["content-type"] == "text/event-stream"
        assert not traced.get_finished_spans()
        if mode == "unparsed":
            raw.close()
        elif mode == "parse_error":
            # Exercise error ownership without replacing the SDK's raw object.
            monkeypatch.setattr(type(raw.__wrapped__), "parse", lambda *a, **k: (_ for _ in ()).throw(error))
            with pytest.raises(httpx.ReadError) as caught:
                raw.parse()
            assert caught.value is error
            raw.close()
        elif mode == "abandon":
            del raw
            gc.collect()
            assert len(traced.get_finished_spans()) == 1
            assert traced.get_finished_spans()[0].status.status_code == StatusCode.UNSET
            return
        else:
            stream = raw.parse()
            assert raw.parse() is stream
            assert isinstance(stream, openai.Stream)
            if mode == "detach":
                del raw
                gc.collect()
                assert not traced.get_finished_spans()
            if mode == "error":
                with pytest.raises((httpx.ReadError, openai.APIConnectionError)) as caught:
                    list(stream)
                assert caught.value is error or caught.value.__cause__ is error
            elif mode in ("early", "raw_close"):
                next(stream)
            else:
                assert len(list(stream)) == 2
            if mode == "raw_close":
                raw.close()
            stream.close()
            if mode != "detach":
                raw.close()
            del stream
        if mode != "detach":
            del raw
        gc.collect()
    assert_finished(traced, body, error=mode in ("error", "parse_error"), complete=mode in ("exhaust", "detach"))


async def parse_async(raw):
    # LegacyAPIResponse.parse is synchronous even for the asynchronous SDK.
    parsed = raw.parse()
    return await parsed if inspect.isawaitable(parsed) else parsed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exhaust", "early", "raw_close", "unparsed", "error", "parse_error", "detach", "abandon"])
async def test_async_raw_stream_ownership(traced, mode, monkeypatch):
    error = httpx.ReadError("synthetic stream failure")
    body = AsyncBody(error=error if mode == "error" else None)
    async with async_client(body) as client:
        raw = await client.chat.completions.with_raw_response.create(**PARAMS, stream=True)
        assert raw.status_code == 200
        assert raw.headers["content-type"] == "text/event-stream"
        assert not traced.get_finished_spans()
        if mode == "unparsed":
            await raw.close()
        elif mode == "parse_error":
            monkeypatch.setattr(type(raw.__wrapped__), "parse", lambda *a, **k: (_ for _ in ()).throw(error))
            with pytest.raises(httpx.ReadError) as caught:
                await parse_async(raw)
            assert caught.value is error
            await raw.close()
        elif mode == "abandon":
            del raw
            gc.collect()
            assert len(traced.get_finished_spans()) == 1
            assert traced.get_finished_spans()[0].status.status_code == StatusCode.UNSET
            return
        else:
            stream = await parse_async(raw)
            assert await parse_async(raw) is stream
            assert isinstance(stream, openai.AsyncStream)
            if mode == "detach":
                del raw
                gc.collect()
                assert not traced.get_finished_spans()
            if mode == "error":
                with pytest.raises((httpx.ReadError, openai.APIConnectionError)) as caught:
                    _ = [chunk async for chunk in stream]
                assert caught.value is error or caught.value.__cause__ is error
            elif mode in ("early", "raw_close"):
                await stream.__anext__()
            else:
                assert len([chunk async for chunk in stream]) == 2
            if mode == "raw_close":
                await raw.close()
            await stream.close()
            if mode != "detach":
                await raw.close()
            del stream
        if mode != "detach":
            del raw
        gc.collect()
    assert_finished(traced, body, error=mode in ("error", "parse_error"), complete=mode in ("exhaust", "detach"))


@pytest.mark.parametrize("message_kind", ["mapping", "object", "model"])
def test_image_privacy_for_message_objects(message_kind):
    from types import SimpleNamespace
    from pydantic import BaseModel
    from traceai_fireworks._wrappers import _get_input_attributes

    class Image(BaseModel):
        type: str = "image_url"
        image_url: dict

    class Message(BaseModel):
        role: str = "user"
        content: list

    sentinel = "SYNTHETIC-OBJECT-IMAGE"
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + sentinel}}]
    if message_kind == "mapping":
        message = {"role": "user", "content": content}
    elif message_kind == "object":
        message = SimpleNamespace(role="user", content=content)
    else:
        message = Message(content=[Image(**content[0])])
    attrs = _get_input_attributes(MODEL, [message], config=TraceConfig(hide_input_images=True))
    assert sentinel not in str(attrs)
    assert sentinel in str(message)


@pytest.mark.parametrize("error", [False, True])
def test_explicit_finish_detaches_finalizer(error):
    native, span = MagicMock(), MagicMock()
    if error:
        native.close.side_effect = RuntimeError("synthetic close failure")
    stream = _Stream(native, span)
    if error:
        with pytest.raises(RuntimeError):
            stream.close()
    else:
        stream.close()
    del stream
    gc.collect()
    span.end.assert_called_once_with()
    assert span.set_status.call_args.args[0].status_code == (StatusCode.ERROR if error else StatusCode.OK)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [False, True])
async def test_async_explicit_finish_detaches_finalizer(error):
    from unittest.mock import AsyncMock

    native, span = MagicMock(), MagicMock()
    native.close = AsyncMock(side_effect=RuntimeError("synthetic close failure") if error else None)
    stream = _AsyncStream(native, span)
    if error:
        with pytest.raises(RuntimeError):
            await stream.close()
    else:
        await stream.close()
    del stream
    gc.collect()
    span.end.assert_called_once_with()
    assert span.set_status.call_args.args[0].status_code == (StatusCode.ERROR if error else StatusCode.OK)


def test_streaming_response_context(traced):
    body = SyncBody()
    with sync_client(body) as client:
        if not hasattr(client.chat.completions, "with_streaming_response"):
            pytest.skip("Streaming response context was added after OpenAI 1.0")
        with client.chat.completions.with_streaming_response.create(**PARAMS, stream=True) as raw:
            assert raw.status_code == 200
            stream = raw.parse()
            assert raw.parse() is stream
            assert len(list(stream)) == 2
    assert_finished(traced, body, complete=True)


@pytest.mark.asyncio
async def test_async_streaming_response_context(traced):
    body = AsyncBody()
    async with async_client(body) as client:
        if not hasattr(client.chat.completions, "with_streaming_response"):
            pytest.skip("Streaming response context was added after OpenAI 1.0")
        async with client.chat.completions.with_streaming_response.create(**PARAMS, stream=True) as raw:
            assert raw.status_code == 200
            stream = await raw.parse()
            assert await raw.parse() is stream
            assert len([chunk async for chunk in stream]) == 2
    assert_finished(traced, body, complete=True)
