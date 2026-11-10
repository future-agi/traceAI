"""Defer native raw-response tracing until parsing/consumption or cleanup.

No network body is fetched by instrumentation. Response and parsed stream share the same
completion state, so their independent close paths can only export one span.
"""

import json
from typing import Any

import httpx
from groq import AsyncStream, Stream
from groq.types.chat import ChatCompletion, ChatCompletionChunk
from wrapt import ObjectProxy

from traceai_groq._stream import _AsyncStream, _Stream, _StreamState


class _ResponseProxy(ObjectProxy):
    def __init__(self, response: Any, state: _StreamState) -> None:
        super().__init__(response)
        self._self_state = state
        self._self_streams = {}

    def _self_read_complete(self) -> bool:
        return self.__wrapped__.is_closed and not self._self_state.finished

    def _self_replay_response(self, body: bytes) -> httpx.Response:
        native = self.__wrapped__.http_response
        return httpx.Response(
            native.status_code,
            headers=native.headers,
            content=body,
            request=native.request,
        )

    def _self_finish_json(self, body: bytes) -> None:
        # Only replay a fully consumed body. Do not populate the SDK's parse
        # cache, advance its iterator or alter the chunks returned to the caller.
        try:
            data = json.loads(body)
        except Exception as error:
            self._self_state.finish(error)
            return
        try:
            parsed = ChatCompletion.model_validate(data)
        except Exception:
            # The SDK can accept loosely typed JSON. Telemetry validation must
            # not turn a successful native read into a caller-visible error.
            self._self_state.finish()
        else:
            self._self_state.finish_response(parsed)

    def _self_wrap_parsed(self, parsed: Any, proxy_type: type) -> Any:
        # The SDK caches parse() results per target type. Preserve that identity
        # without modifying its private cache or wrapping the same stream twice.
        key = id(parsed)
        if key not in self._self_streams:
            self._self_streams[key] = proxy_type(parsed, self._self_state)
        return self._self_streams[key]


class _RawResponse(_ResponseProxy):
    def _self_finish_read(self) -> None:
        if not self._self_read_complete():
            return
        if self._self_state.request_parameters.get("stream", False):
            # Reparse HTTPX's cached body using the SDK's SSE decoder. _parse()
            # creates an independent stream; the caller's parse() cache and
            # subsequent iteration are untouched, even after a full prefetch.
            try:
                for chunk in self.__wrapped__._parse():
                    self._self_state.capture(chunk)
            except Exception:
                # Telemetry cannot change the result of a successful native read.
                pass
            self._self_state.finish()
        else:
            # A completed HTTPX read retains bytes for the SDK's later parse().
            # Use an independent parse for telemetry without changing its cache.
            try:
                parsed = self.__wrapped__._parse()
            except Exception as error:
                # read() still succeeds, but a later native parse() of this
                # same body would fail. Do not export premature OK telemetry.
                self._self_state.finish(error)
            else:
                self._self_state.finish_response(parsed)

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
        # Collect only what the caller actually consumes; never advance its
        # iterator or close its response early. The native SDK decodes SSE.
        parts = []
        try:
            for part in getattr(self.__wrapped__, method)(*args, **kwargs):
                parts.append(
                    part
                    if method == "iter_bytes"
                    else part.encode("utf-8")
                    + (b"\n" if method == "iter_lines" else b"")
                )
                yield part
            if self._self_state.request_parameters.get("stream"):
                try:
                    stream = Stream(
                        cast_to=ChatCompletionChunk,
                        response=self._self_replay_response(b"".join(parts)),
                        client=self.__wrapped__._client,
                    )
                    for chunk in stream:
                        self._self_state.capture(chunk)
                except Exception:
                    pass  # Telemetry must not change a successful native read.
            else:
                self._self_finish_json(b"".join(parts))
        except GeneratorExit:
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            parts.clear()
            self._self_state.finish()


class _AsyncRawResponse(_ResponseProxy):
    async def _self_finish_read(self) -> None:
        if not self._self_read_complete():
            return
        if self._self_state.request_parameters.get("stream", False):
            try:
                async for chunk in self.__wrapped__._parse():
                    self._self_state.capture(chunk)
            except Exception:
                pass
            self._self_state.finish()
        else:
            try:
                parsed = self.__wrapped__._parse()
            except Exception as error:
                # read() still succeeds, but a later native parse() of this
                # same body would fail. Do not export premature OK telemetry.
                self._self_state.finish(error)
            else:
                self._self_state.finish_response(parsed)

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
        await self._self_finish_read()
        return result

    async def text(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._self_call("text", *args, **kwargs)
        await self._self_finish_read()
        return result

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._self_call("json", *args, **kwargs)
        await self._self_finish_read()
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
        parts = []
        try:
            async for part in getattr(self.__wrapped__, method)(*args, **kwargs):
                parts.append(
                    part
                    if method == "iter_bytes"
                    else part.encode("utf-8")
                    + (b"\n" if method == "iter_lines" else b"")
                )
                yield part
            if self._self_state.request_parameters.get("stream"):
                try:
                    stream = AsyncStream(
                        cast_to=ChatCompletionChunk,
                        response=self._self_replay_response(b"".join(parts)),
                        client=self.__wrapped__._client,
                    )
                    async for chunk in stream:
                        self._self_state.capture(chunk)
                except Exception:
                    pass  # Telemetry must not change a successful native read.
            else:
                self._self_finish_json(b"".join(parts))
        except GeneratorExit:
            raise
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            parts.clear()
            self._self_state.finish()
