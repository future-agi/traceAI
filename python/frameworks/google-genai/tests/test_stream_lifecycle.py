"""Closing a GenAI stream must finish its partial span."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry import trace as trace_api

from fi_instrumentation.fi_types import SpanAttributes
from traceai_google_genai._stream import _Stream
from traceai_google_genai._with_span import _WithSpan


class _Chunk:
    def __init__(self, text):
        self.text = text

    def model_dump(self, **kwargs):
        return {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": self.text}]}}
            ]
        }


def _make_stream(provider):
    span = Mock(spec=trace_api.Span)
    span.is_recording.return_value = True
    # Spy on the real span lifetime as well as the stream's finalization count.
    with_span = Mock(wraps=_WithSpan(span))
    return _Stream(provider, with_span), with_span, span


def _assert_finished(with_span, span, text=None, status_code=None):
    with_span.finish_tracing.assert_called_once()
    span.end.assert_called_once_with()
    kwargs = with_span.finish_tracing.call_args.kwargs
    if text is None:
        assert SpanAttributes.OUTPUT_VALUE not in kwargs["attributes"]
    else:
        output = json.loads(kwargs["attributes"][SpanAttributes.OUTPUT_VALUE])
        assert output["candidates"][0]["content"]["parts"][0]["text"] == text
    if status_code is None:
        assert kwargs["status"] is None
    else:
        assert kwargs["status"].status_code == status_code


def _sync_provider(closed, error=None):
    try:
        yield _Chunk("hello")
        if error is not None:
            raise error
        yield _Chunk(" world")
    finally:
        closed.append(True)


async def _async_provider(closed, error=None):
    try:
        yield _Chunk("hello")
        if error is not None:
            raise error
        yield _Chunk(" world")
    finally:
        closed.append(True)


@pytest.mark.parametrize("close_target", ["iterator", "stream"])
def test_sync_early_close_finishes_partial_span_once(close_target):
    closed = []
    provider = _sync_provider(closed)
    stream, with_span, span = _make_stream(provider)
    iterator = iter(stream)
    try:
        assert next(iterator).text == "hello"
        (iterator if close_target == "iterator" else stream).close()
        _assert_finished(with_span, span, "hello")
        assert closed == [True]
        iterator.close()
        stream.close()
        _assert_finished(with_span, span, "hello")
    finally:
        iterator.close()
        provider.close()


@pytest.mark.parametrize("close_target", ["iterator", "stream"])
def test_async_early_close_finishes_partial_span_once(close_target):
    async def run():
        closed = []
        provider = _async_provider(closed)
        stream, with_span, span = _make_stream(provider)
        iterator = stream.__aiter__()
        try:
            assert (await iterator.__anext__()).text == "hello"
            await (iterator if close_target == "iterator" else stream).aclose()
            _assert_finished(with_span, span, "hello")
            assert closed == [True]
            await iterator.aclose()
            await stream.aclose()
            _assert_finished(with_span, span, "hello")
        finally:
            await iterator.aclose()
            await provider.aclose()

    asyncio.run(run())


def test_async_cancellation_finishes_partial_span_and_propagates():
    async def run():
        waiting = asyncio.Event()
        closed = []

        async def provider():
            try:
                yield _Chunk("hello")
                waiting.set()
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        source = provider()
        stream, with_span, span = _make_stream(source)
        iterator = stream.__aiter__()
        assert (await iterator.__anext__()).text == "hello"
        pending = asyncio.create_task(iterator.__anext__())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            # Check before asyncio.run's automatic async-generator cleanup.
            _assert_finished(with_span, span, "hello")
            assert closed == [True]
            await stream.aclose()
            _assert_finished(with_span, span, "hello")
        finally:
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            await iterator.aclose()
            await source.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True], ids=["full", "provider-error"])
def test_sync_completion_and_errors_keep_status_and_finish_once(fails):
    closed = []
    error = ValueError("fake provider error") if fails else None
    stream, with_span, span = _make_stream(_sync_provider(closed, error))
    iterator = iter(stream)
    assert next(iterator).text == "hello"
    if fails:
        with pytest.raises(ValueError) as caught:
            next(iterator)
        assert caught.value is error
        span.record_exception.assert_called_once_with(error)
    else:
        assert [chunk.text for chunk in iterator] == [" world"]
        span.record_exception.assert_not_called()
    assert closed == [True]
    stream.close()
    _assert_finished(
        with_span,
        span,
        "hello" if fails else "hello world",
        trace_api.StatusCode.ERROR if fails else trace_api.StatusCode.OK,
    )


@pytest.mark.parametrize("fails", [False, True], ids=["full", "provider-error"])
def test_async_completion_and_errors_keep_status_and_finish_once(fails):
    async def run():
        closed = []
        error = ValueError("fake provider error") if fails else None
        stream, with_span, span = _make_stream(_async_provider(closed, error))
        iterator = stream.__aiter__()
        assert (await iterator.__anext__()).text == "hello"
        if fails:
            with pytest.raises(ValueError) as caught:
                await iterator.__anext__()
            assert caught.value is error
            span.record_exception.assert_called_once_with(error)
        else:
            assert [chunk.text async for chunk in iterator] == [" world"]
            span.record_exception.assert_not_called()
        assert closed == [True]
        await stream.aclose()
        _assert_finished(
            with_span,
            span,
            "hello" if fails else "hello world",
            trace_api.StatusCode.ERROR if fails else trace_api.StatusCode.OK,
        )

    asyncio.run(run())


def test_close_before_iteration_finishes_span():
    provider = _sync_provider([])
    stream, with_span, span = _make_stream(provider)
    stream.close()
    stream.close()
    assert provider.gi_frame is None
    _assert_finished(with_span, span)


def test_aclose_before_iteration_finishes_span():
    async def run():
        provider = _async_provider([])
        stream, with_span, span = _make_stream(provider)
        await stream.aclose()
        await stream.aclose()
        assert provider.ag_frame is None
        _assert_finished(with_span, span)

    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True], ids=["result", "cleanup-error"])
def test_close_preserves_provider_result_or_error_and_finishes(fails):
    result = object()
    error = RuntimeError("fake close error")
    close = Mock(return_value=result, side_effect=error if fails else None)
    stream, with_span, span = _make_stream(SimpleNamespace(close=close))
    if fails:
        with pytest.raises(RuntimeError) as caught:
            stream.close()
        assert caught.value is error
    else:
        assert stream.close() is result
    close.assert_called_once_with()
    _assert_finished(with_span, span)


@pytest.mark.parametrize("fails", [False, True], ids=["result", "cleanup-error"])
def test_aclose_preserves_provider_result_or_error_and_finishes(fails):
    async def run():
        result = object()
        error = RuntimeError("fake aclose error")
        close = AsyncMock(return_value=result, side_effect=error if fails else None)
        stream, with_span, span = _make_stream(SimpleNamespace(aclose=close))
        if fails:
            with pytest.raises(RuntimeError) as caught:
                await stream.aclose()
            assert caught.value is error
        else:
            assert await stream.aclose() is result
        close.assert_awaited_once_with()
        _assert_finished(with_span, span)

    asyncio.run(run())
