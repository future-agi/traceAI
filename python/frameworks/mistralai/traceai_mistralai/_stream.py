from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Generator, Iterator, Optional, Protocol, Tuple

if TYPE_CHECKING:
    from mistralai.models import CompletionEvent
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue
from traceai_mistralai._utils import _finish_tracing
from traceai_mistralai._with_span import _WithSpan
from wrapt import ObjectProxy

__all__ = (
    "_Stream",
    "_ResponseAccumulator",
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ResponseAccumulator(Protocol):
    def process_chunk(self, chunk: Any) -> None: ...

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]: ...

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]: ...


class _Stream(ObjectProxy):  # type: ignore
    __slots__ = (
        "_self_with_span",
        "_self_iteration_count",
        "_self_is_finished",
        "_self_response_accumulator",
    )

    def __init__(
        self,
        stream: Optional[Generator[CompletionEvent, None, None]],
        with_span: _WithSpan,
        response_accumulator: Optional[_ResponseAccumulator] = None,
    ) -> None:
        super().__init__(stream)
        self._self_with_span = with_span
        self._self_iteration_count = 0
        self._self_is_finished = with_span.is_finished
        self._self_response_accumulator = response_accumulator

    def __iter__(self) -> Iterator[Any]:
        return self

    def __enter__(self):
        if hasattr(self.__wrapped__, "__enter__"):
            self.__wrapped__.__enter__()
        return self

    def close(self):
        try:
            closer = getattr(self.__wrapped__, "close", None)
            if closer is not None:
                return closer()
            return self.__wrapped__.__exit__(None, None, None)
        finally:
            if not self._self_is_finished:
                self._finish_tracing(trace_api.Status(trace_api.StatusCode.OK))

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self._self_with_span.record_exception(exc_val)
            self._finish_tracing(trace_api.Status(trace_api.StatusCode.ERROR))
        self.close()
        return False

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
            except Exception:
                logger.exception("Failed to accumulate response")

    def _finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
    ) -> None:
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


class _AsyncStream(ObjectProxy):  # type: ignore
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

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __aenter__(self):
        if hasattr(self.__wrapped__, "__aenter__"):
            await self.__wrapped__.__aenter__()
        return self

    async def close(self):
        try:
            closer = getattr(self.__wrapped__, "aclose", None) or getattr(self.__wrapped__, "close", None)
            if closer is not None:
                return await closer()
            return await self.__wrapped__.__aexit__(None, None, None)
        finally:
            if not self._self_is_finished:
                self._finish_tracing(trace_api.Status(trace_api.StatusCode.OK))

    async def aclose(self):
        return await self.close()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self._self_with_span.record_exception(exc_val)
            self._finish_tracing(trace_api.Status(trace_api.StatusCode.ERROR))
        await self.close()
        return False

    async def __anext__(self) -> Any:
        # Pass through mistaken calls
        if not hasattr(self.__wrapped__, "__anext__"):
            return await self.__wrapped__.__anext__()

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
