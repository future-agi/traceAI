"""Native SDK direct/mixed stream advancement and in-flight close ownership."""
import asyncio
import json

import anthropic
import pytest
from opentelemetry.trace import StatusCode

from test_r3_stream_resume import EVENTS, PARAMS, SECRET, assert_span, httpx, make_client, setup


def test_mixed_sync_direct_iter_resume():
    client = make_client(EVENTS)
    provider, exporter, instrumentor = setup()
    try:
        stream = client.messages.create(**PARAMS)
        assert next(stream).type == 'message_start'
        iterator = iter(stream)
        assert next(iterator).type == 'content_block_start'
        assert next(stream).type == 'content_block_delta'
        iterator.close()
        assert not exporter.get_finished_spans()
        assert [event.type for event in stream] == ['content_block_stop', 'message_delta', 'message_stop']
        assert_span(exporter)
        with pytest.raises(StopIteration):
            next(stream)
        assert_span(exporter)
    finally:
        instrumentor.uninstrument(); client.close(); provider.shutdown()


def test_mixed_async_direct_iter_resume():
    async def run():
        client = make_client(EVENTS, True)
        provider, exporter, instrumentor = setup()
        try:
            stream = await client.messages.create(**PARAMS)
            assert (await stream.__anext__()).type == 'message_start'
            iterator = stream.__aiter__()
            assert (await iterator.__anext__()).type == 'content_block_start'
            assert (await stream.__anext__()).type == 'content_block_delta'
            await iterator.aclose()
            assert not exporter.get_finished_spans()
            assert [event.type async for event in stream] == ['content_block_stop', 'message_delta', 'message_stop']
            assert_span(exporter)
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert_span(exporter)
        finally:
            instrumentor.uninstrument(); await client.close(); provider.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('asynchronous', [False, True])
def test_direct_partial_close(asynchronous):
    async def run():
        client = make_client(EVENTS, True)
        provider, exporter, instrumentor = setup()
        try:
            stream = await client.messages.create(**PARAMS)
            for _ in range(3):
                await stream.__anext__()
            assert not exporter.get_finished_spans()
            assert await stream.close() is None
            assert_span(exporter, full=False)
            attrs = exporter.get_finished_spans()[0].attributes
            assert 'hello' in str(attrs)
            assert attrs['gen_ai.usage.input_tokens'] == 4
        finally:
            instrumentor.uninstrument(); await client.close(); provider.shutdown()
    if asynchronous:
        asyncio.run(run()); return
    client = make_client(EVENTS)
    provider, exporter, instrumentor = setup()
    try:
        stream = client.messages.create(**PARAMS)
        for _ in range(3):
            next(stream)
        assert not exporter.get_finished_spans()
        assert stream.close() is None
        assert_span(exporter, full=False)
        attrs = exporter.get_finished_spans()[0].attributes
        assert 'hello' in str(attrs)
        assert attrs['gen_ai.usage.input_tokens'] == 4
    finally:
        instrumentor.uninstrument(); client.close(); provider.shutdown()


@pytest.mark.parametrize('traced', [False, True])
@pytest.mark.parametrize('outcome', ['error', 'cancel'])
def test_close_returns_before_pending_error_and_multiple_closers(traced, outcome, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', 'true')
    class Wire(httpx.AsyncByteStream):
        def __init__(self):
            self.pending = asyncio.Event(); self.release = asyncio.Event()
        async def __aiter__(self):
            yield ('event: message_start\ndata: ' + json.dumps(EVENTS[0]) + '\n\n').encode()
            self.pending.set()
            await self.release.wait()
            raise RuntimeError(SECRET)
        async def aclose(self):
            pass
    async def run():
        wire = Wire()
        client = anthropic.AsyncAnthropic(api_key='local', http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=wire))))
        if traced:
            provider, exporter, instrumentor = setup()
        try:
            stream = await client.messages.create(**PARAMS)
            assert (await stream.__anext__()).type == 'message_start'
            pending = asyncio.create_task(stream.__anext__())
            await asyncio.wait_for(wire.pending.wait(), 2)
            assert await asyncio.wait_for(asyncio.gather(stream.close(), stream.close()), 2) == [None, None]
            if traced:
                assert not exporter.get_finished_spans()
            if outcome == 'cancel':
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                wire.release.set()
                with pytest.raises(RuntimeError, match=SECRET):
                    await pending
            if traced:
                assert_span(exporter, error=True, full=False)
        finally:
            if traced:
                instrumentor.uninstrument(); provider.shutdown()
            await client.close()
    asyncio.run(run())


def test_close_exception_preserves_native_error_and_return():
    from traceai_anthropic._stream import _MessagesStream
    from traceai_anthropic._with_span import _WithSpan
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    class Native:
        def close(self):
            raise ValueError('native close failure')
    provider = TracerProvider(); exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    stream = _MessagesStream(Native(), _WithSpan(provider.get_tracer(__name__).start_span('close')))
    try:
        with pytest.raises(ValueError, match='native close failure'):
            stream.close()
        assert len(exporter.get_finished_spans()) == 1
        assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR
    finally:
        provider.shutdown()


@pytest.mark.parametrize('traced', [False, True])
def test_sync_close_does_not_finish_before_pending_error(traced, monkeypatch):
    import threading
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', 'true')
    class Wire(httpx.SyncByteStream):
        def __init__(self):
            self.pending = threading.Event(); self.release = threading.Event()
        def __iter__(self):
            yield ('event: message_start\ndata: ' + json.dumps(EVENTS[0]) + '\n\n').encode()
            self.pending.set()
            if not self.release.wait(3):
                raise TimeoutError('test release timed out')
            raise RuntimeError(SECRET)
        def close(self):
            pass
    wire = Wire()
    client = anthropic.Anthropic(api_key='local', http_client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=wire))))
    if traced:
        provider, exporter, instrumentor = setup()
    try:
        stream = client.messages.create(**PARAMS)
        assert next(stream).type == 'message_start'
        errors = []
        def advance():
            try:
                next(stream)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=advance)
        thread.start()
        assert wire.pending.wait(2)
        assert stream.close() is None
        assert stream.close() is None
        if traced:
            assert not exporter.get_finished_spans()
        wire.release.set(); thread.join(2)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == SECRET
        if traced:
            assert_span(exporter, error=True, full=False)
    finally:
        wire.release.set()
        if traced:
            instrumentor.uninstrument(); provider.shutdown()
        client.close()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_direct_advance_context_exception(asynchronous, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', 'true')
    async def run():
        client = make_client(EVENTS, True); provider, exporter, instrumentor = setup()
        try:
            with pytest.raises(ValueError, match=SECRET):
                async with await client.messages.create(**PARAMS) as stream:
                    assert (await stream.__anext__()).type == 'message_start'
                    raise ValueError(SECRET)
            assert_span(exporter, error=True, full=False)
        finally:
            instrumentor.uninstrument(); await client.close(); provider.shutdown()
    if asynchronous:
        asyncio.run(run()); return
    client = make_client(EVENTS); provider, exporter, instrumentor = setup()
    try:
        with pytest.raises(ValueError, match=SECRET):
            with client.messages.create(**PARAMS) as stream:
                assert next(stream).type == 'message_start'
                raise ValueError(SECRET)
        assert_span(exporter, error=True, full=False)
    finally:
        instrumentor.uninstrument(); client.close(); provider.shutdown()
