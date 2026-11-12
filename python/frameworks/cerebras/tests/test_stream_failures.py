"""Transport cleanup failures must not mask iteration errors or leave spans open."""

import pytest
from opentelemetry.trace import StatusCode
from traceai_cerebras._wrappers import _AsyncStream, _CompletionsWrapper, _Stream


class BrokenStream:
    def __iter__(self):
        return self

    def __next__(self):
        raise ValueError("iteration failure")

    def close(self):
        raise RuntimeError("cleanup failure")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BrokenAsyncStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ValueError("iteration failure")

    async def close(self):
        raise RuntimeError("cleanup failure")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


@pytest.mark.parametrize("operation", ["close", "exit", "iterate"])
def test_sync_cleanup_failure(mock_tracer, operation):
    span = mock_tracer.start_span("fixture")
    stream = _Stream(BrokenStream(), span)
    expected = ValueError if operation == "iterate" else RuntimeError
    with pytest.raises(expected):
        if operation == "iterate":
            next(stream)
        elif operation == "exit":
            with stream:
                pass
        else:
            stream.close()
    assert span._ended
    assert span.status.status_code == StatusCode.ERROR
    assert len(span.events) == 1
    assert isinstance(span.events[0]["exception"], expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close", "exit", "iterate"])
async def test_async_cleanup_failure(mock_tracer, operation):
    span = mock_tracer.start_span("fixture")
    stream = _AsyncStream(BrokenAsyncStream(), span)
    expected = ValueError if operation == "iterate" else RuntimeError
    with pytest.raises(expected):
        if operation == "iterate":
            await stream.__anext__()
        elif operation == "exit":
            async with stream:
                pass
        else:
            await stream.close()
    assert span._ended
    assert span.status.status_code == StatusCode.ERROR
    assert len(span.events) == 1
    assert isinstance(span.events[0]["exception"], expected)


def test_response_extraction_fails_open_without_logging_payload(mock_tracer, caplog):
    import logging

    class MalformedResponse:
        @property
        def choices(self):
            raise ValueError("sensitive-response")

    response = MalformedResponse()
    wrapper = _CompletionsWrapper(mock_tracer)
    with caplog.at_level(logging.DEBUG, logger="traceai_cerebras._wrappers"):
        assert (
            wrapper(
                lambda **kwargs: response,
                None,
                (),
                {"model": "fixture", "messages": []},
            )
            is response
        )
    assert "ValueError" in caplog.text
    assert "sensitive-response" not in caplog.text
    assert mock_tracer.spans[0]._ended
