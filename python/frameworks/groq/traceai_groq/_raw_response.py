"""Defer native raw-response tracing until parsing/consumption or cleanup.

No body is read by instrumentation. Response and parsed stream share the same
completion state, so their independent close paths can only export one span.
"""

from typing import Any

from wrapt import ObjectProxy

from traceai_groq._stream import _AsyncStream, _Stream, _StreamState


class _ResponseProxy(ObjectProxy):
    def __init__(self, response: Any, state: _StreamState) -> None:
        super().__init__(response)
        self._self_state = state
        self._self_streams = {}

    def _self_wrap_parsed(self, parsed: Any, proxy_type: type) -> Any:
        # The SDK caches parse() results per target type. Preserve that identity
        # without modifying its private cache or wrapping the same stream twice.
        key = id(parsed)
        if key not in self._self_streams:
            self._self_streams[key] = proxy_type(parsed, self._self_state)
        return self._self_streams[key]


class _RawResponse(_ResponseProxy):
    def parse(self, *args: Any, **kwargs: Any) -> Any:
        parsed = self._self_call("parse", *args, **kwargs)
        return self._self_wrap_parsed(parsed, _Stream)

    def _self_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.__wrapped__, method)(*args, **kwargs)
        except BaseException as error:
            self._self_state.finish(error)
            raise

    def read(self, *args: Any, **kwargs: Any) -> Any:
        # read() may prefetch the bytes before parse(); do not end a successful
        # span here. Parsing/iteration or explicit close owns finalization.
        return self._self_call("read", *args, **kwargs)

    def text(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_call("text", *args, **kwargs)

    def json(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_call("json", *args, **kwargs)

    def close(self) -> Any:
        try:
            return self._self_call("close")
        finally:
            self._self_state.finish()

    def iter_bytes(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_iterate("iter_bytes", *args, **kwargs)

    def iter_text(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_iterate("iter_text", *args, **kwargs)

    def iter_lines(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_iterate("iter_lines", *args, **kwargs)

    def _self_iterate(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # Low-level bytes/text are not ChatCompletionChunks; retain lifecycle
        # and errors, but never pass those values to the chunk extractor.
        try:
            yield from getattr(self.__wrapped__, method)(*args, **kwargs)
        except GeneratorExit:
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish()


class _AsyncRawResponse(_ResponseProxy):
    async def parse(self, *args: Any, **kwargs: Any) -> Any:
        parsed = await self._self_call("parse", *args, **kwargs)
        return self._self_wrap_parsed(parsed, _AsyncStream)

    async def _self_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await getattr(self.__wrapped__, method)(*args, **kwargs)
        except BaseException as error:
            self._self_state.finish(error)
            raise

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        return await self._self_call("read", *args, **kwargs)

    async def text(self, *args: Any, **kwargs: Any) -> Any:
        return await self._self_call("text", *args, **kwargs)

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        return await self._self_call("json", *args, **kwargs)

    async def close(self) -> Any:
        try:
            return await self._self_call("close")
        finally:
            self._self_state.finish()

    def iter_bytes(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_iterate("iter_bytes", *args, **kwargs)

    def iter_text(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_iterate("iter_text", *args, **kwargs)

    def iter_lines(self, *args: Any, **kwargs: Any) -> Any:
        return self._self_iterate("iter_lines", *args, **kwargs)

    async def _self_iterate(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            async for chunk in getattr(self.__wrapped__, method)(*args, **kwargs):
                yield chunk
        except GeneratorExit:
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish()
