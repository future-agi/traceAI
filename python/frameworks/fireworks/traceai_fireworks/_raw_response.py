"""Lazy raw-response ownership, including the OpenAI 1.0 SDK floor.

Like the shared OpenAI adapter, preserve the raw API and cache the instrumented
parse result. Importing that adapter here would require its newer SDK floor.
"""

import inspect
from typing import Any, Optional

from opentelemetry.trace import Span

from traceai_fireworks._stream import _AsyncStream, _Stream, _StreamBase


class _RawStreamResponse(_StreamBase):
    def __init__(self, response: Any, span: Span, asynchronous: bool) -> None:
        super().__init__(response, span)
        self._self_asynchronous = asynchronous
        self._self_parsed = None
        self._self_parsed_raw = None

    def _wrap_parsed(self, parsed: Any) -> Any:
        if self._self_parsed is not None and parsed is self._self_parsed_raw:
            return self._self_parsed
        proxy = _AsyncStream if self._self_asynchronous else _Stream
        wrapped = proxy(parsed, self._self_state.span, state=self._self_state)
        self._self_parsed_raw = parsed
        self._self_parsed = wrapped
        return wrapped

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            parsed = self.__wrapped__.parse(*args, **kwargs)
            if inspect.isawaitable(parsed):
                async def await_parsed() -> Any:
                    try:
                        return self._wrap_parsed(await parsed)
                    except BaseException as error:
                        self._finish(error)
                        raise
                return await_parsed()
            return self._wrap_parsed(parsed)
        except BaseException as error:
            self._finish(error)
            raise

    def close(self) -> Any:
        return self.aclose() if self._self_asynchronous else self._close()

    def _close(self, error: Optional[BaseException] = None) -> Any:
        if self._self_closed:
            return None
        self._self_closed = True
        try:
            if self._self_parsed is not None:
                return self._self_parsed._close(error)
            close = getattr(self.__wrapped__, "close", None)
            return (close or self.__wrapped__.http_response.close)()
        except BaseException as close_error:
            if error is None:
                error = close_error
            raise
        finally:
            self._finish(error)

    async def aclose(self, error: Optional[BaseException] = None) -> Any:
        if self._self_closed:
            return None
        self._self_closed = True
        try:
            if self._self_parsed is not None:
                return await self._self_parsed._close(error)
            close = getattr(self.__wrapped__, "close", None)
            result = (close or self.__wrapped__.http_response.aclose)()
            return await result if inspect.isawaitable(result) else result
        except BaseException as close_error:
            if error is None:
                error = close_error
            raise
        finally:
            self._finish(error)

    def __enter__(self) -> "_RawStreamResponse":
        try:
            enter = getattr(self.__wrapped__, "__enter__", None)
            if enter is not None:
                enter()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._close(exc)

    async def __aenter__(self) -> "_RawStreamResponse":
        try:
            enter = getattr(self.__wrapped__, "__aenter__", None)
            if enter is not None:
                await enter()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self.aclose(exc)
