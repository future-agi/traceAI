"""Research iterator lifecycle on tavily-python's real sync/async clients."""

import asyncio

import pytest
from fi_instrumentation import TraceConfig, suppress_tracing
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from _support import attrs, instrumented
from _tavily_fake import TAVILY_KEY
from test_extended_operations import (
    CHUNKS, PRIVATE_OUTPUT, PRIVATE_URL, QUESTION, REQUEST_ID, Transport,
)


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("split", [False, True])
def test_stream_is_lazy_parented_and_preserves_every_byte(monkeypatch, is_async, split):
    transport = Transport()
    if split:
        transport.chunks = [bytes([byte]) for byte in b"".join(CHUNKS)]

    with instrumented() as traced:
        tracer = traced.provider.get_tracer("test")

        async def consume_async():
            client = await transport.async_client()
            try:
                with tracer.start_as_current_span("parent") as parent:
                    stream = await client.research(QUESTION, stream=True)
                    # Upstream's async stream performs no HTTP until iterated.
                    assert not transport.calls
                    assert not traced.spans()
                with tracer.start_as_current_span("consumer") as consumer:
                    result = []
                    async for chunk in stream:
                        result.append(chunk)
                        assert trace.get_current_span() is consumer
                        assert not any(s.name == "tavily.research" for s in traced.spans())
                    await stream.aclose()
                return result, parent
            finally:
                await client.close()

        if is_async:
            result, parent = asyncio.run(consume_async())
        else:
            client = transport.sync_client(monkeypatch)
            with tracer.start_as_current_span("parent") as parent:
                stream = client.research(QUESTION, stream=True)
                assert not traced.spans()
            with tracer.start_as_current_span("consumer") as consumer:
                result = []
                for chunk in stream:
                    result.append(chunk)
                    assert trace.get_current_span() is consumer
                    assert not any(s.name == "tavily.research" for s in traced.spans())
                stream.close()
        assert result == transport.chunks
        spans = [s for s in traced.spans() if s.name == "tavily.research"]
        assert len(spans) == 1
        span = spans[0]
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code == StatusCode.OK
        assert attrs(span)["tavily.request_id"] == REQUEST_ID
        assert all(context.span_id == span.context.span_id for context in transport.contexts)
        assert transport.closed
        for private in (PRIVATE_OUTPUT, PRIVATE_URL, TAVILY_KEY):
            assert private not in traced.wire()


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("started", [False, True])
def test_explicit_close_ends_once_without_claiming_completion(monkeypatch, is_async, started):
    transport = Transport()
    with instrumented() as traced:
        async def run():
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                if started:
                    assert await stream.asend(None) == CHUNKS[0]
                await stream.aclose()
                await stream.aclose()
            finally:
                await client.close()

        if is_async:
            asyncio.run(run())
        else:
            stream = transport.sync_client(monkeypatch).research(QUESTION, stream=True)
            if started:
                assert stream.send(None) == CHUNKS[0]
            stream.close()
            stream.close()
        span = traced.one()
        assert attrs(span)["tavily.stream.closed"] is True
        assert span.status.status_code == StatusCode.UNSET
        assert not span.events
        if started:
            assert transport.closed
        assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("is_async", [False, True])
def test_midstream_error_preserves_sdk_error_and_closes_transport(monkeypatch, is_async):
    transport = Transport()
    error = ValueError(f"{QUESTION} {PRIVATE_URL} {PRIVATE_OUTPUT} {TAVILY_KEY}")
    transport.error = error
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        async def run():
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                async for _ in stream:
                    pass
            finally:
                await client.close()

        with pytest.raises(Exception) as caught:
            if is_async:
                asyncio.run(run())
            else:
                list(transport.sync_client(monkeypatch).research(QUESTION, stream=True))
        # Tavily's async generator wraps read errors; sync leaves them intact.
        if is_async:
            assert caught.value.__context__ is error
        else:
            assert caught.value is error
        assert PRIVATE_OUTPUT in str(caught.value)
        span = traced.one()
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) == 1
        assert set(span.events[0].attributes) == {"exception.type"}
        assert transport.closed
        for private in (QUESTION, PRIVATE_URL, PRIVATE_OUTPUT, TAVILY_KEY):
            assert private not in traced.wire()


def test_async_stream_cancellation_ends_once_and_closes_http_response():
    transport = Transport()
    with instrumented() as traced:
        async def run():
            transport.wait = asyncio.Event()
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                assert await anext(stream) == CHUNKS[0]
                assert await anext(stream) == CHUNKS[1]
                task = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await stream.aclose()
            finally:
                await client.close()
        asyncio.run(run())
        span = traced.one()
        assert attrs(span)["tavily.cancelled"] is True
        assert span.status.status_code == StatusCode.ERROR
        assert not span.events
        assert transport.closed
        assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("is_async", [False, True])
def test_throw_protocol_preserves_vendor_exception(monkeypatch, is_async):
    transport = Transport()
    error = ValueError("consumer exception")
    with instrumented() as traced:
        async def run():
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                await anext(stream)
                await stream.athrow(error)
            finally:
                await client.close()
        with pytest.raises(Exception) as caught:
            if is_async:
                asyncio.run(run())
            else:
                stream = transport.sync_client(monkeypatch).research(QUESTION, stream=True)
                next(stream)
                stream.throw(error)
        assert (caught.value.__context__ if is_async else caught.value) is error
        assert traced.one().status.status_code == StatusCode.ERROR
        assert transport.closed


@pytest.mark.parametrize("is_async", [False, True])
def test_suppressed_stream_does_not_export(monkeypatch, is_async):
    transport = Transport()
    with instrumented() as traced:
        async def run():
            client = await transport.async_client()
            try:
                with suppress_tracing():
                    stream = await client.research(QUESTION, stream=True)
                return [chunk async for chunk in stream]
            finally:
                await client.close()
        if is_async:
            result = asyncio.run(run())
        else:
            with suppress_tracing():
                stream = transport.sync_client(monkeypatch).research(QUESTION, stream=True)
            result = list(stream)
        assert result == CHUNKS
        assert traced.spans() == []


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("hide_inputs", [False, True])
def test_bounded_stream_metadata_and_hide_inputs(monkeypatch, is_async, hide_inputs):
    transport = Transport()
    transport.chunks = [
        b'data: {"request_id":"' + TAVILY_KEY.encode() + b'"}\n\n',
        b'data: {"request_id":"' + PRIVATE_URL.encode() + b'"}\n\n',
        b'data: {"request_id":"' + b"x" * 10000 + b'"}\n\n',
        b'data: invalid-json\n\ndata: ["not-an-object"]\n\n',
        *CHUNKS,
    ]
    with instrumented(config=TraceConfig(hide_inputs=hide_inputs)) as traced:
        async def run():
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                return [chunk async for chunk in stream]
            finally:
                await client.close()
        if is_async:
            result = asyncio.run(run())
        else:
            result = list(transport.sync_client(monkeypatch).research(QUESTION, stream=True))
        assert result == transport.chunks
        if hide_inputs:
            assert "tavily.request_id" not in attrs(traced.one())
            assert QUESTION not in traced.wire()
            assert REQUEST_ID not in traced.wire()
        else:
            assert attrs(traced.one())["tavily.request_id"] == REQUEST_ID
        for private in (PRIVATE_URL, PRIVATE_OUTPUT, TAVILY_KEY):
            assert private not in traced.wire()


@pytest.mark.parametrize("is_async", [False, True])
def test_stream_http_error_is_recorded_at_the_sdk_failure_point(monkeypatch, is_async):
    transport = Transport()
    transport.status = 400
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        async def run():
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                assert not traced.spans()
                await anext(stream)
            finally:
                await client.close()
        with pytest.raises(Exception):
            if is_async:
                asyncio.run(run())
            else:
                transport.sync_client(monkeypatch).research(QUESTION, stream=True)
        span = traced.one()
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) == 1
        for private in (QUESTION, PRIVATE_URL, PRIVATE_OUTPUT, TAVILY_KEY):
            assert private not in traced.wire()


@pytest.mark.parametrize("is_async", [False, True])
def test_rejected_initial_send_does_not_end_resumable_stream(monkeypatch, is_async):
    transport = Transport()
    with instrumented() as traced:
        async def run():
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                with pytest.raises(TypeError):
                    await stream.asend("invalid-first-send")
                assert traced.spans() == []
                return [chunk async for chunk in stream]
            finally:
                await client.close()
        if is_async:
            result = asyncio.run(run())
        else:
            stream = transport.sync_client(monkeypatch).research(QUESTION, stream=True)
            with pytest.raises(TypeError):
                stream.send("invalid-first-send")
            assert traced.spans() == []
            result = list(stream)
        assert result == CHUNKS
        assert traced.one().status.status_code == StatusCode.OK


def test_concurrent_advance_rejection_does_not_finish_active_stream():
    transport = Transport()
    with instrumented() as traced:
        async def run():
            transport.wait = asyncio.Event()
            client = await transport.async_client()
            try:
                stream = await client.research(QUESTION, stream=True)
                await anext(stream)
                await anext(stream)
                pending = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                try:
                    with pytest.raises(RuntimeError):
                        await anext(stream)
                    assert traced.spans() == []
                finally:
                    transport.wait.set()
                    with pytest.raises(StopAsyncIteration):
                        await pending
            finally:
                await client.close()
        asyncio.run(run())
        assert traced.one().status.status_code == StatusCode.OK
        assert transport.closed
