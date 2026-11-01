"""Lazy research streams: one span, active only inside the vendor iterator."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Generator
from typing import Any, Callable, Optional, Sequence

from opentelemetry.trace import Span

from traceai_tavily._wrappers import _Operation, _attach, _detach, _end

logger = logging.getLogger(__name__)
_MAX_LINE = 4096


class _ResearchStream:
    def __init__(
        self, stream: Any, operation: _Operation, span: Span,
        keys: Optional[Sequence[str]], query: Optional[str],
    ) -> None:
        self._stream = stream
        self._operation = operation
        self._span = span
        self._keys = keys
        self._query = query
        self._done = False
        self._line = b""
        self._oversized = False

    def _observe(self, chunk: Any) -> None:
        """Read only a bounded SSE data line for its top-level request ID.

        Never buffer a response or modify bytes delivered to the caller. Lines
        can cross chunks; oversized lines and non-JSON data are simply ignored.
        """
        try:
            if not isinstance(chunk, bytes) or self._done:
                return
            offset = 0
            while offset < len(chunk):
                newline = chunk.find(b"\n", offset)
                end = len(chunk) if newline < 0 else newline
                if not self._oversized:
                    if len(self._line) + end - offset <= _MAX_LINE:
                        self._line += chunk[offset:end]
                    else:
                        self._line = b""
                        self._oversized = True
                if newline < 0:
                    break
                if not self._oversized and self._line.startswith(b"data:"):
                    try:
                        data = json.loads(self._line[5:].strip())
                    except (ValueError, UnicodeError):
                        pass
                    else:
                        self._operation._request_id(self._span, data, self._keys)
                self._line = b""
                self._oversized = False
                offset = newline + 1
        except Exception:
            logger.debug("Could not read research stream metadata", exc_info=True)

    def _finish(self, error: Optional[BaseException] = None, closed: bool = False) -> None:
        if self._done:
            return
        self._done = True
        self._line = b""
        if closed or isinstance(error, GeneratorExit):
            try:
                self._span.set_attribute("tavily.stream.closed", True)
            except Exception:
                logger.debug("Could not record research stream close", exc_info=True)
            # Closing early is not successful exhaustion or a vendor error.
            _end(self._span)
        elif error is not None:
            self._operation._finish_error(self._span, error, self._keys, self._query)
        else:
            self._operation._finish_ok(self._span, None)

    def __del__(self) -> None:
        # Best effort for abandoned streams; callers should close/aclose them.
        self._finish(closed=True)


class _SyncResearchStream(_ResearchStream, Generator):
    def __iter__(self) -> _SyncResearchStream:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def _advance(self, call: Callable[..., Any], *args: Any, closed: bool = False) -> Any:
        token = _attach(self._span) if not self._done else None
        try:
            result = call(*args)
        except StopIteration:
            self._finish()
            raise
        except BaseException as error:
            # Invalid send/concurrent iteration can fail without closing the
            # SDK generator. It may still be resumed, so keep its span open.
            if self._stream.gi_frame is None:
                self._finish(error)
            raise
        else:
            if closed:
                self._finish(closed=True)
            else:
                self._observe(result)
            return result
        finally:
            _detach(token)

    def send(self, value: Any) -> Any:
        return self._advance(self._stream.send, value)

    def throw(self, *args: Any) -> Any:
        return self._advance(self._stream.throw, *args)

    def close(self) -> Any:
        return self._advance(self._stream.close, closed=True)


class _AsyncResearchStream(_ResearchStream, AsyncGenerator):
    def __aiter__(self) -> _AsyncResearchStream:
        return self

    async def __anext__(self) -> Any:
        return await self.asend(None)

    async def _advance(self, call: Callable[..., Any], *args: Any, closed: bool = False) -> Any:
        token = _attach(self._span) if not self._done else None
        try:
            result = await call(*args)
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as error:
            if self._stream.ag_frame is None:
                self._finish(error)
            raise
        else:
            if closed:
                self._finish(closed=True)
            else:
                self._observe(result)
            return result
        finally:
            _detach(token)

    async def asend(self, value: Any) -> Any:
        return await self._advance(self._stream.asend, value)

    async def athrow(self, *args: Any) -> Any:
        return await self._advance(self._stream.athrow, *args)

    async def aclose(self) -> Any:
        return await self._advance(self._stream.aclose, closed=True)
