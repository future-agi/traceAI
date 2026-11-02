"""Defer native raw-response tracing until parsing/consumption or cleanup.

No network body is fetched by instrumentation. Response and parsed stream share the same
completion state, so their independent close paths can only export one span.
"""

import json
from typing import Any

from wrapt import ObjectProxy

from traceai_groq._stream import _AsyncStream, _Stream, _StreamState


class _ResponseProxy(ObjectProxy):
    def __init__(self, response: Any, state: _StreamState) -> None:
        super().__init__(response)
        self._self_state = state
        self._self_streams = {}

    def _self_finish_read(self) -> None:
        if not self.__wrapped__.is_closed or self._self_state.finished:
            return
        if self._self_state.request_parameters.get("stream", False):
            # The SDK/HTTPX has already cached the fully read body. Decode SSE
            # from that cache without consuming the SDK stream returned by parse().
            # This retains prefetch-then-parse and its cached stream identity.
            try:
                data = []
                for line in self.__wrapped__.http_response.content.splitlines():
                    if line.startswith(b"data:"):
                        data.append(line[5:].strip())
                    elif not line and data:
                        payload = b"\n".join(data)
                        if payload != b"[DONE]":
                            self._self_state.capture(json.loads(payload))
                        data.clear()
                if data and b"\n".join(data) != b"[DONE]":
                    self._self_state.capture(json.loads(b"\n".join(data)))
            except Exception:
                # Telemetry must never alter a successful native read.
                pass
        self._self_state.finish()

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
        if not self._self_state.request_parameters.get("stream", False):
            self._self_state.finish_response(parsed)
            return parsed
        return self._self_wrap_parsed(parsed, _Stream)

    def _self_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.__wrapped__, method)(*args, **kwargs)
        except BaseException as error:
            self._self_state.finish(error)
            raise

    def read(self, *args: Any, **kwargs: Any) -> Any:
        result = self._self_call("read", *args, **kwargs)
        self._self_finish_read()
        return result

    def text(self, *args: Any, **kwargs: Any) -> Any:
        result = self._self_call("text", *args, **kwargs)
        self._self_finish_read()
        return result

    def json(self, *args: Any, **kwargs: Any) -> Any:
        result = self._self_call("json", *args, **kwargs)
        self._self_finish_read()
        return result

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
        if not self._self_state.request_parameters.get("stream", False):
            self._self_state.finish_response(parsed)
            return parsed
        return self._self_wrap_parsed(parsed, _AsyncStream)

    async def _self_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await getattr(self.__wrapped__, method)(*args, **kwargs)
        except BaseException as error:
            self._self_state.finish(error)
            raise

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._self_call("read", *args, **kwargs)
        self._self_finish_read()
        return result

    async def text(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._self_call("text", *args, **kwargs)
        self._self_finish_read()
        return result

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._self_call("json", *args, **kwargs)
        self._self_finish_read()
        return result

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
