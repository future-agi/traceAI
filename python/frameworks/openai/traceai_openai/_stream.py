import json
import logging
from typing import Any, AsyncIterator, Callable, Generator, Iterator, Optional, Protocol, Tuple

from openai.types.chat import ChatCompletionChunk
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue
from traceai_openai._response_accumulator import (
    _ResponsesAccumulator,
    _raw_response_status,
    _response_status,
)
from traceai_openai._utils import _finish_tracing
from traceai_openai._with_span import _WithSpan
from wrapt import ObjectProxy

__all__ = (
    "_Stream",
    "_ResponseAccumulator",
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ResponseAccumulator(Protocol):
    def process_chunk(self, chunk: Any) -> None: ...

    def get_status(self) -> Optional[trace_api.Status]: ...

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]: ...

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]: ...


def _finish_on_exit(stream: Any, error: Optional[BaseException]) -> None:
    """Finish even on an early context exit, without ending an exhausted span twice."""
    if stream._self_is_finished:
        return
    if error is None:
        status = trace_api.Status(status_code=trace_api.StatusCode.OK)
    else:
        stream._self_with_span.record_exception(error)
        status = trace_api.Status(
            status_code=trace_api.StatusCode.ERROR,
            description=f"{type(error).__name__}: {error}",
        )
    stream._finish_tracing(status=status)


class _ResponseCloseProxy(ObjectProxy):
    """Responses.stream() closes the HTTP response, not the raw SDK stream.

    Expose a per-call proxy instead of patching HTTPX classes or the client's
    transport. Internal SDK reads still use the original response unchanged.
    """

    def __init__(self, response: Any, stream: Any) -> None:
        super().__init__(response)
        self._self_stream = stream

    def close(self) -> None:
        error = None
        try:
            self.__wrapped__.close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self._self_stream, error)

    async def aclose(self) -> None:
        error = None
        try:
            await self.__wrapped__.aclose()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self._self_stream, error)


class _RawBodyBuffer:
    """Bounded inspection of chunks the caller consumed, never an extra HTTP read.

    HTTPX iterators do not populate response.content. Retain at most 64 KiB for
    status inspection, then discard it: an oversized/opaque body has unknown
    generation status, not an affirmative success. No body is exported.
    """

    _LIMIT = 64 * 1024

    def __init__(self) -> None:
        self._body: Optional[bytearray] = bytearray()

    def add(self, chunk: Any) -> None:
        if self._body is None:
            return
        # Check character count before encoding so a huge text chunk cannot
        # allocate an unbounded temporary copy. Then enforce the byte limit too.
        remaining = self._LIMIT - len(self._body)
        if len(chunk) > remaining:
            self._body = None
            return
        data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if len(data) > remaining:
            self._body = None
        else:
            self._body.extend(data)

    def take(self) -> Any:
        body, self._body = self._body, None
        try:
            return json.loads(body) if body is not None else None
        except (ValueError, UnicodeError):
            return None


class _RawResponse(ObjectProxy):
    """Trace every parse-cache entry without eagerly reading a lazy envelope."""

    def __init__(
        self,
        response: Any,
        with_span: _WithSpan,
        response_accumulator: Optional[_ResponseAccumulator],
        on_parse: Callable[[Any], Any],
    ) -> None:
        super().__init__(response)
        self._self_with_span = with_span
        self._self_response_accumulator = response_accumulator
        self._self_on_parse = on_parse
        self._self_parsed = {}
        self._self_body_status: Optional[trace_api.Status] = None

    def _body_buffer(self) -> Optional[_RawBodyBuffer]:
        if (
            isinstance(self._self_response_accumulator, _ResponsesAccumulator)
            and not self._self_is_finished
            and not getattr(self.__wrapped__, "_is_sse_stream", False)
        ):
            return _RawBodyBuffer()
        return None

    def _record_body_consumption(self, buffer: Optional[_RawBodyBuffer] = None) -> None:
        # read/text/json consume a non-SSE envelope without calling parse().
        # Do not force SDK parsing (or its post_parser) on a raw-body caller.
        # SSE still needs a terminal event from the traced iterator.
        if (
            self._self_is_finished
            or self._self_body_status is not None
            or getattr(self.__wrapped__, "_is_sse_stream", False)
        ):
            return
        try:
            body = buffer.take() if buffer is not None else self.__wrapped__.http_response.json()
        except Exception:
            body = None  # Successful raw reads need not contain JSON.
        self._self_body_status = (
            _raw_response_status(body)
            if isinstance(self._self_response_accumulator, _ResponsesAccumulator)
            else _response_status(body)
        )

    def _consume_body(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            result = getattr(self.__wrapped__, method)(*args, **kwargs)
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        self._record_body_consumption()
        return result

    def read(self, *args: Any, **kwargs: Any) -> Any:
        return self._consume_body("read", *args, **kwargs)

    def text(self, *args: Any, **kwargs: Any) -> Any:
        return self._consume_body("text", *args, **kwargs)

    def json(self, *args: Any, **kwargs: Any) -> Any:
        return self._consume_body("json", *args, **kwargs)

    def _consume_iterator(self, method: str, *args: Any, **kwargs: Any) -> Iterator[Any]:
        buffer = self._body_buffer()
        try:
            for chunk in getattr(self.__wrapped__, method)(*args, **kwargs):
                if buffer is not None:
                    buffer.add(chunk)
                    if method == "iter_lines":
                        buffer.add(b"\n")
                yield chunk
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        else:
            # The caller may stop iteration early; only exhaustion consumes it.
            self._record_body_consumption(buffer)

    def iter_bytes(self, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        return self._consume_iterator("iter_bytes", *args, **kwargs)

    def iter_text(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        return self._consume_iterator("iter_text", *args, **kwargs)

    def iter_lines(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        return self._consume_iterator("iter_lines", *args, **kwargs)

    def stream_to_file(self, *args: Any, **kwargs: Any) -> None:
        self._consume_body("stream_to_file", *args, **kwargs)

    @property
    def _self_is_finished(self) -> bool:
        return self._self_with_span.is_finished

    def _trace_parsed(self, parsed: Any) -> Any:
        # The SDK owns cache keys (including generic stream types). Cache by
        # identity and retain the original through the proxy, so repeated parse
        # calls return the same wrapper without collapsing distinct SDK entries.
        key = id(parsed)
        if key not in self._self_parsed:
            self._self_parsed[key] = self._self_on_parse(parsed)
        return self._self_parsed[key]

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            parsed = self.__wrapped__.parse(*args, **kwargs)
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        return self._trace_parsed(parsed)

    @property
    def http_response(self) -> Any:
        return _ResponseCloseProxy(self.__wrapped__.http_response, self)

    def close(self) -> Any:
        if any(isinstance(parsed, _AsyncStream) for parsed in self._self_parsed.values()):
            return self.aclose()
        error = None
        try:
            # LegacyAPIResponse has no close method. Preserve the convenience
            # close path already exposed by our raw-stream wrapper on dev.
            close = getattr(self.__wrapped__, "close", None)
            if close is None:
                close = self.__wrapped__.http_response.close
            close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self, error)

    async def aclose(self) -> None:
        error = None
        try:
            await self.__wrapped__.http_response.aclose()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self, error)

    def _finish_tracing(self, status: Optional[trace_api.Status] = None) -> None:
        if self._self_is_finished:
            return
        accumulator = self._self_response_accumulator
        if status is None or status.status_code is not trace_api.StatusCode.ERROR:
            get_status = getattr(accumulator, "get_status", None)
            if self._self_body_status is not None:
                status = self._self_body_status
            elif get_status is not None:
                status = get_status() or status
            elif not self._self_parsed:
                status = trace_api.Status(
                    trace_api.StatusCode.ERROR, "Response closed before parsing"
                )
        if accumulator is not None:
            _finish_tracing(self._self_with_span, accumulator, status)
        else:
            self._self_with_span.finish_tracing(status=status)


class _LegacyRawResponse(_RawResponse):
    """Legacy envelopes expose text as a property, including on async clients."""

    @property
    def text(self) -> str:
        try:
            result = self.__wrapped__.text
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        self._record_body_consumption()
        return result


class _AsyncRawResponse(_RawResponse):
    """AsyncAPIResponse has awaitable parse/close; LegacyAPIResponse does not."""

    async def _consume_body(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            result = await getattr(self.__wrapped__, method)(*args, **kwargs)
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        self._record_body_consumption()
        return result

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        return await self._consume_body("read", *args, **kwargs)

    async def text(self, *args: Any, **kwargs: Any) -> Any:
        return await self._consume_body("text", *args, **kwargs)

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        return await self._consume_body("json", *args, **kwargs)

    async def _consume_iterator(self, method: str, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        buffer = self._body_buffer()
        try:
            async for chunk in getattr(self.__wrapped__, method)(*args, **kwargs):
                if buffer is not None:
                    buffer.add(chunk)
                    if method == "iter_lines":
                        buffer.add(b"\n")
                yield chunk
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        else:
            self._record_body_consumption(buffer)

    async def iter_bytes(self, *args: Any, **kwargs: Any) -> AsyncIterator[bytes]:
        async for chunk in self._consume_iterator("iter_bytes", *args, **kwargs):
            yield chunk

    async def iter_text(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        async for chunk in self._consume_iterator("iter_text", *args, **kwargs):
            yield chunk

    async def iter_lines(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        async for chunk in self._consume_iterator("iter_lines", *args, **kwargs):
            yield chunk

    async def stream_to_file(self, *args: Any, **kwargs: Any) -> None:
        await self._consume_body("stream_to_file", *args, **kwargs)

    async def parse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            parsed = await self.__wrapped__.parse(*args, **kwargs)
        except BaseException as exc:
            _finish_on_exit(self, exc)
            raise
        return self._trace_parsed(parsed)

    async def close(self) -> None:
        error = None
        try:
            await self.__wrapped__.close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self, error)


class _Stream(ObjectProxy):  # type: ignore
    __slots__ = (
        "_self_with_span",
        "_self_iteration_count",
        "_self_is_finished",
        "_self_response_accumulator",
    )

    def __init__(
        self,
        stream: Optional[Generator[ChatCompletionChunk, None, None]],
        with_span: _WithSpan,
        response_accumulator: Optional[_ResponseAccumulator] = None,
    ) -> None:
        super().__init__(stream)
        self._self_with_span = with_span
        self._self_iteration_count = 0
        self._self_is_finished = with_span.is_finished
        self._self_response_accumulator = response_accumulator

    @property
    def response(self) -> Any:
        response = self.__wrapped__.response
        if getattr(self._self_response_accumulator, "get_status", None) is not None:
            return _ResponseCloseProxy(response, self)
        return response

    def __enter__(self) -> "_Stream":
        # ObjectProxy otherwise returns the SDK stream, bypassing our iterator.
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return self.__wrapped__.__exit__(exc_type, exc, tb)
        except BaseException as close_error:
            exc = close_error
            raise
        finally:
            _finish_on_exit(self, exc)

    def close(self) -> None:
        error = None
        try:
            self.__wrapped__.close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self, error)

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        try:
            chunk = self.__wrapped__.__next__()
        except StopIteration:
            if not self._self_is_finished:
                status = trace_api.Status(status_code=trace_api.StatusCode.OK)
                self._finish_tracing(status=status)
            raise
        except BaseException as exception:
            if not self._self_is_finished:
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                self._self_with_span.record_exception(exception)
                self._finish_tracing(status=status)
            raise
        else:
            self._process_chunk(chunk)
            return chunk

    def _process_chunk(self, chunk: Any) -> None:
        if not self._self_iteration_count:
            try:
                self._self_with_span.add_event("First Token Stream Event")
            except Exception:
                logger.exception("Failed to add event to span")
        self._self_iteration_count += 1
        if self._self_response_accumulator is not None:
            try:
                self._self_response_accumulator.process_chunk(chunk)
            except Exception as e:
                logger.exception("Failed to accumulate response")

    def _finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
    ) -> None:
        if self._self_is_finished:
            return
        if status is None or status.status_code is not trace_api.StatusCode.ERROR:
            get_status = getattr(self._self_response_accumulator, "get_status", None)
            if get_status is not None:
                status = get_status() or status
        _finish_tracing(
            status=status,
            with_span=self._self_with_span,
            has_attributes=self,
        )
        self._self_is_finished = True

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if self._self_response_accumulator is not None:
            yield from self._self_response_accumulator.get_attributes()

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if self._self_response_accumulator is not None:
            yield from self._self_response_accumulator.get_extra_attributes()


class _AsyncStream(ObjectProxy):
    __slots__ = (
        "_self_with_span",
        "_self_iteration_count",
        "_self_is_finished",
        "_self_response_accumulator",
    )

    def __init__(
        self,
        stream: AsyncIterator[Any],
        with_span: _WithSpan,
        response_accumulator: Optional[_ResponseAccumulator] = None,
    ) -> None:
        super().__init__(stream)
        self._self_with_span = with_span
        self._self_iteration_count = 0
        self._self_is_finished = with_span.is_finished
        self._self_response_accumulator = response_accumulator

    @property
    def response(self) -> Any:
        response = self.__wrapped__.response
        if getattr(self._self_response_accumulator, "get_status", None) is not None:
            return _ResponseCloseProxy(response, self)
        return response

    async def __aenter__(self) -> "_AsyncStream":
        await self.__wrapped__.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self.__wrapped__.__aexit__(exc_type, exc, tb)
        except BaseException as close_error:
            exc = close_error
            raise
        finally:
            _finish_on_exit(self, exc)

    async def close(self) -> None:
        error = None
        try:
            await self.__wrapped__.close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_on_exit(self, error)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self.__wrapped__.__anext__()
        except StopAsyncIteration:
            if not self._self_is_finished:
                status = trace_api.Status(status_code=trace_api.StatusCode.OK)
                self._finish_tracing(status=status)
            raise
        except BaseException as exception:
            if not self._self_is_finished:
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                self._self_with_span.record_exception(exception)
                self._finish_tracing(status=status)
            raise
        else:
            self._process_chunk(chunk)
            return chunk

    def _process_chunk(self, chunk: Any) -> None:
        if not self._self_iteration_count:
            try:
                self._self_with_span.add_event("First Token Stream Event")
            except Exception:
                logger.exception("Failed to add event to span")
        self._self_iteration_count += 1
        if self._self_response_accumulator is not None:
            try:
                self._self_response_accumulator.process_chunk(chunk)
            except Exception:
                logger.exception("Failed to accumulate response")

    def _finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
    ) -> None:
        if self._self_is_finished:
            return
        if status is None or status.status_code is not trace_api.StatusCode.ERROR:
            get_status = getattr(self._self_response_accumulator, "get_status", None)
            if get_status is not None:
                status = get_status() or status
        _finish_tracing(
            status=status,
            with_span=self._self_with_span,
            has_attributes=self,
        )
        self._self_is_finished = True

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if self._self_response_accumulator is not None:
            yield from self._self_response_accumulator.get_attributes()

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if self._self_response_accumulator is not None:
            yield from self._self_response_accumulator.get_extra_attributes()
