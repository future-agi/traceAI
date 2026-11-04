"""Exact-once ownership and exception identity at the SDK/proxy boundary."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.trace import StatusCode

from traceai_fireworks._wrappers import (
    _AsyncCompletionsWrapper,
    _CompletionsWrapper,
    _get_input_attributes,
)


def test_allowlisted_request_fields_exclude_unknown_transport_options():
    tools = [{"type": "function", "function": {"name": "weather"}}]
    attributes = _get_input_attributes(
        "synthetic-model", [], tools=tools, tool_choice="auto",
        response_format={"type": "json_object"},
        future_transport_option={"credential": "SYNTHETIC-UNKNOWN-SECRET"},
    )
    assert "SYNTHETIC-UNKNOWN-SECRET" not in str(attributes)
    raw = json.loads(attributes["input.value"])
    assert raw["tools"] == tools
    assert raw["tool_choice"] == "auto"
    assert raw["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("close_error", [None, RuntimeError("synthetic close error")])
def test_sync_close_ends_once_even_when_cleanup_raises(close_error):
    native, span = MagicMock(), MagicMock()
    native.close.side_effect = close_error
    stream = _CompletionsWrapper(MagicMock())._wrap_stream(native, span)
    if close_error:
        with pytest.raises(RuntimeError) as caught:
            stream.close()
        assert caught.value is close_error
        span.record_exception.assert_called_once_with(close_error)
    else:
        stream.close()
    stream.close()
    span.end.assert_called_once_with()
    native.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_error", [None, RuntimeError("synthetic close error")])
async def test_async_close_ends_once_even_when_cleanup_raises(close_error):
    native, span = MagicMock(), MagicMock()
    native.close = AsyncMock(side_effect=close_error)
    stream = _AsyncCompletionsWrapper(MagicMock())._wrap_async_stream(native, span)
    if close_error:
        with pytest.raises(RuntimeError) as caught:
            await stream.close()
        assert caught.value is close_error
        span.record_exception.assert_called_once_with(close_error)
    else:
        await stream.close()
    await stream.aclose()
    span.end.assert_called_once_with()
    native.close.assert_awaited_once_with()


def test_read_error_is_not_replaced_by_close_error():
    error = RuntimeError("synthetic read error")
    native, span = MagicMock(), MagicMock()
    native.__next__.side_effect = error
    native.close.side_effect = ValueError("synthetic close error")
    stream = _CompletionsWrapper(MagicMock())._wrap_stream(native, span)
    with pytest.raises(RuntimeError) as caught:
        next(stream)
    assert caught.value is error
    span.record_exception.assert_called_once_with(error)
    assert span.set_status.call_args.args[0].status_code == StatusCode.ERROR
    span.end.assert_called_once_with()
    native.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_request_cancellation_preserves_exception_identity(mock_fireworks_client, stream):
    # Task cancellation messages are not propagated to awaiters on Python 3.10.
    # Exercise the wrapper boundary directly to assert the stronger invariant.
    error = asyncio.CancelledError("synthetic request cancellation")
    wrapped, tracer = AsyncMock(side_effect=error), MagicMock()
    span = tracer.start_span.return_value
    wrapper = _AsyncCompletionsWrapper(tracer)
    with pytest.raises(asyncio.CancelledError) as caught:
        await wrapper(wrapped, mock_fireworks_client, (), {
            "model": "synthetic-model", "messages": [], "stream": stream,
        })
    assert caught.value is error
    span.record_exception.assert_called_once_with(error)
    span.end.assert_called_once_with()


@pytest.mark.asyncio
async def test_stream_cancellation_is_not_replaced_by_close_error():
    error = asyncio.CancelledError("synthetic cancellation")
    native, span = MagicMock(), MagicMock()
    native.__anext__ = AsyncMock(side_effect=error)
    native.close = AsyncMock(side_effect=ValueError("synthetic close error"))
    stream = _AsyncCompletionsWrapper(MagicMock())._wrap_async_stream(native, span)
    with pytest.raises(asyncio.CancelledError) as caught:
        await stream.__anext__()
    assert caught.value is error
    span.record_exception.assert_called_once_with(error)
    assert span.set_status.call_args.args[0].status_code == StatusCode.ERROR
    span.end.assert_called_once_with()
    native.close.assert_awaited_once_with()
