"""Stream proxies preserve SDK iteration, closing and context-manager APIs."""

import logging
from typing import Any, Mapping, Optional

from opentelemetry import trace as trace_api
from wrapt import ObjectProxy

from traceai_together._with_span import _WithSpan

logger = logging.getLogger(__name__)


class _StreamState:
    def __init__(
        self,
        span: _WithSpan,
        accumulator: Any,
        extractor: Any,
        request: Mapping[str, Any],
    ) -> None:
        self.span = span
        self.accumulator = accumulator
        self.extractor = extractor
        self.request = request
        self.first_chunk = True

    def process(self, chunk: Any) -> None:
        try:
            if self.first_chunk:
                self.span.add_event("First Token Stream Event")
                self.first_chunk = False
            self.accumulator.process_chunk(chunk)
        except Exception:
            # Telemetry must not interrupt a successful SDK stream.
            logger.exception("Failed to accumulate Together stream chunk")

    def finish(self, error: Optional[BaseException] = None, completed: bool = False) -> None:
        if self.span.is_finished:
            return
        if error is not None:
            self.span.record_exception(error)
            self.span.finish_tracing(status=trace_api.Status(
                trace_api.StatusCode.ERROR, f"{type(error).__name__}: {error}"
            ))
            return
        status = trace_api.Status(trace_api.StatusCode.OK if completed else trace_api.StatusCode.UNSET)
        try:
            response = self.accumulator.get_accumulated_response()
            self.span.finish_tracing(
                status=status,
                attributes=dict(self.extractor.get_attributes(response, is_streaming=True)),
                extra_attributes=dict(self.extractor.get_extra_attributes(response, self.request, is_streaming=True)),
            )
        except Exception:
            logger.exception("Failed to finalize Together stream")
            self.span.finish_tracing(status=status)


class _Stream(ObjectProxy):
    def __init__(self, response: Any, state: _StreamState) -> None:
        super().__init__(response)
        self._self_state = state

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self.__wrapped__)
        except StopIteration:
            self._self_state.finish(completed=True)
            raise
        except BaseException as error:
            self._self_state.finish(error=error)
            raise
        self._self_state.process(chunk)
        return chunk

    def close(self):
        try:
            return self.__wrapped__.close()
        finally:
            self._self_state.finish()

    def __enter__(self):
        enter = getattr(self.__wrapped__, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            exit_ = getattr(self.__wrapped__, "__exit__", None)
            if exit_ is not None:
                return exit_(exc_type, exc, tb)
            self.__wrapped__.close()
        finally:
            self._self_state.finish(error=exc)


class _AsyncStream(ObjectProxy):
    def __init__(self, response: Any, state: _StreamState) -> None:
        super().__init__(response)
        self._self_state = state

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self.__wrapped__.__anext__()
        except StopAsyncIteration:
            self._self_state.finish(completed=True)
            raise
        except BaseException as error:
            self._self_state.finish(error=error)
            raise
        self._self_state.process(chunk)
        return chunk

    async def close(self):
        try:
            close = getattr(self.__wrapped__, "aclose", None) or self.__wrapped__.close
            return await close()
        finally:
            self._self_state.finish()

    async def aclose(self):
        return await self.close()

    async def __aenter__(self):
        enter = getattr(self.__wrapped__, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            exit_ = getattr(self.__wrapped__, "__aexit__", None)
            if exit_ is not None:
                return await exit_(exc_type, exc, tb)
            close = getattr(self.__wrapped__, "aclose", None) or self.__wrapped__.close
            await close()
        finally:
            self._self_state.finish(error=exc)


def _wrap_stream(response: Any, state: _StreamState, proxy: type) -> Any:
    """Proxy SDK streams; finish the span for raw responses that are not streams."""
    if hasattr(response, "__anext__" if proxy is _AsyncStream else "__next__"):
        return proxy(response, state)
    state.span.finish_tracing(status=trace_api.Status(trace_api.StatusCode.OK))
    return response
