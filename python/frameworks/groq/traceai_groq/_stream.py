"""Stream proxies that retain the SDK's response and resource lifecycle."""

import logging
from typing import Any, Mapping, Optional

from opentelemetry import trace as trace_api
from traceai_groq._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_groq._utils import _finish_tracing
from traceai_groq._with_span import _WithSpan
from wrapt import ObjectProxy

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _StreamState:
    def __init__(
        self,
        span: _WithSpan,
        extractor: _ResponseAttributesExtractor,
        request_parameters: Mapping[str, Any],
    ) -> None:
        self.span = span
        self.extractor = extractor
        self.request_parameters = request_parameters
        self.chunks = []
        self.finished = span.is_finished

    def capture(self, chunk: Any) -> None:
        if not self.finished:
            self.chunks.append(chunk)

    def finish(self, error: Optional[BaseException] = None) -> None:
        if self.finished:
            return
        # Iteration, explicit close and context exit can all finish one stream.
        self.finished = True
        status = trace_api.Status(
            trace_api.StatusCode.ERROR if error is not None else trace_api.StatusCode.OK
        )
        try:
            if error is not None:
                self.span.record_exception(error)
                try:
                    description = f"{type(error).__name__}: {error}"
                except Exception:
                    description = type(error).__name__
                status = trace_api.Status(trace_api.StatusCode.ERROR, description)
            _finish_tracing(
                with_span=self.span,
                status=status,
                attributes=self.extractor.get_attributes(
                    response=self.chunks, is_streaming=True
                ),
                extra_attributes=self.extractor.get_extra_attributes(
                    response=self.chunks,
                    request_parameters=self.request_parameters,
                    is_streaming=True,
                ),
            )
        except Exception:
            logger.exception("Failed to finalize Groq stream")
        finally:
            # Also end the span if attribute extraction fails, without changing
            # an SDK exception or retaining payloads after the stream is closed.
            self.span.finish_tracing(status=status)
            self.chunks.clear()


class _Stream(ObjectProxy):
    def __init__(self, stream: Any, state: _StreamState) -> None:
        super().__init__(stream)
        self._self_state = state
        self._self_iterator = None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            if self._self_iterator is None:
                self._self_iterator = iter(self.__wrapped__)
            chunk = next(self._self_iterator)
        except StopIteration:
            self._self_state.finish()
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        self._self_state.capture(chunk)
        return chunk

    def __enter__(self):
        try:
            self.__wrapped__.__enter__()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        # Returning the wrapped stream would bypass tracing inside `with`.
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            return self.__wrapped__.__exit__(exc_type, exc, tb)
        except BaseException as error:
            exc = error
            raise
        finally:
            self._self_state.finish(exc)

    def close(self):
        error = None
        try:
            return self.__wrapped__.close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._self_state.finish(error)


class _AsyncStream(ObjectProxy):
    def __init__(self, stream: Any, state: _StreamState) -> None:
        super().__init__(stream)
        self._self_state = state
        self._self_iterator = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            if self._self_iterator is None:
                self._self_iterator = self.__wrapped__.__aiter__()
            chunk = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            self._self_state.finish()
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        self._self_state.capture(chunk)
        return chunk

    async def __aenter__(self):
        try:
            await self.__wrapped__.__aenter__()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self.__wrapped__.__aexit__(exc_type, exc, tb)
        except BaseException as error:
            exc = error
            raise
        finally:
            self._self_state.finish(exc)

    async def close(self):
        error = None
        try:
            close = getattr(self.__wrapped__, "close", None)
            if close is None:
                close = self.__wrapped__.aclose
            return await close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._self_state.finish(error)

    async def aclose(self):
        # Compatibility with the async generator returned by earlier releases;
        # Groq's own AsyncStream spells this method `close`.
        return await self.close()
