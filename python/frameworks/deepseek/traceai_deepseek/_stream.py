"""Compatibility proxies: preserve the OpenAI stream's lifetime and interface."""

import logging
from typing import Any

from opentelemetry import trace
from wrapt import ObjectProxy

from traceai_deepseek._utils import _finish_tracing, _to_dict

logger = logging.getLogger(__name__)


class _StreamState:
    def __init__(self, span: Any, extractor: Any, parameters: Any) -> None:
        self.span = span
        self.extractor = extractor
        self.parameters = parameters
        self.finished = False

    def process(self, chunk: Any) -> None:
        if self.finished:
            return
        try:
            self.extractor.process_chunk(_to_dict(chunk))
        except Exception:
            logger.exception("Failed to accumulate DeepSeek response")

    def finish(self, error: Any = None) -> None:
        if self.finished:
            return
        self.finished = True
        status = trace.Status(trace.StatusCode.OK)
        if error is not None:
            self.span.record_exception(error)
            status = trace.Status(
                trace.StatusCode.ERROR, f"{type(error).__name__}: {error}"
            )
        try:
            _finish_tracing(
                status,
                self.span,
                self.extractor.get_attributes(),
                self.extractor.get_extra_attributes(self.parameters),
            )
        except Exception:
            logger.exception("Failed to finalize DeepSeek stream")
            self.span.finish_tracing(status=status)


class _Stream(ObjectProxy):
    def __init__(self, stream: Any, state: _StreamState) -> None:
        super().__init__(stream)
        self._self_state = state
        self._self_iterator = iter(stream)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._self_iterator)
        except StopIteration:
            self._self_state.finish()
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        self._self_state.process(chunk)
        return chunk

    def __enter__(self):
        if enter := getattr(self.__wrapped__, "__enter__", None):
            enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exit := getattr(self.__wrapped__, "__exit__", None):
                return exit(exc_type, exc, tb)
            self.__wrapped__.response.close()
        except BaseException as error:
            exc = error
            raise
        finally:
            self._self_state.finish(exc)

    def close(self):
        try:
            if close := getattr(self.__wrapped__, "close", None):
                return close()
            return self.__wrapped__.response.close()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish()


class _AsyncStream(ObjectProxy):
    def __init__(self, stream: Any, state: _StreamState) -> None:
        super().__init__(stream)
        self._self_state = state
        self._self_iterator = stream.__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            self._self_state.finish()
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        self._self_state.process(chunk)
        return chunk

    async def __aenter__(self):
        if enter := getattr(self.__wrapped__, "__aenter__", None):
            await enter()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exit := getattr(self.__wrapped__, "__aexit__", None):
                return await exit(exc_type, exc, tb)
            await self.__wrapped__.response.aclose()
        except BaseException as error:
            exc = error
            raise
        finally:
            self._self_state.finish(exc)

    async def close(self):
        try:
            if close := getattr(self.__wrapped__, "close", None):
                return await close()
            return await self.__wrapped__.response.aclose()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish()

    async def aclose(self):
        # Retain the async generator's old close spelling as well as SDK close().
        return await self.close()
