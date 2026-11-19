"""Proxies for the SDK's stream objects: they keep the SDK API (iteration, context manager, close, attributes) and
end the span exactly once when the stream is exhausted, fails, is closed, is left through its context manager,
or is dropped after an early break."""
from typing import Any, Callable, Optional

from wrapt import ObjectProxy

# finish(error, incomplete): ends the span once; error is the exception that stopped the stream, if any.
Finish = Callable[[Optional[BaseException], bool], None]


def _raised(exc_info: Any) -> Optional[BaseException]:
    """The exception a with-block was left with, or None for a normal exit (or one that only closes a generator)."""
    error = exc_info[1] if len(exc_info) > 1 else None
    return error if isinstance(error, BaseException) and not isinstance(error, GeneratorExit) else None


class _StreamState:
    __slots__ = ()

    def _end(self, error: Optional[BaseException] = None, incomplete: bool = False) -> None:
        if self._self_done:
            return
        self._self_done = True
        try:
            self._self_finish(error, incomplete)
        except Exception:  # noqa: BLE001 - tracing never breaks the caller's stream
            pass


class TracedStream(_StreamState, ObjectProxy):
    """Sync stream."""

    def __init__(self, stream: Any, on_chunk: Callable[[Any], None], finish: Finish) -> None:
        super().__init__(stream)
        self._self_on_chunk = on_chunk
        self._self_finish = finish
        self._self_done = False
        self._self_iterator = None

    def __iter__(self) -> "TracedStream":
        return self

    def __next__(self) -> Any:
        if self._self_iterator is None:
            self._self_iterator = iter(self.__wrapped__)
        try:
            chunk = next(self._self_iterator)
        except StopIteration:
            self._end()
            raise
        except BaseException as exc:
            self._end(error=exc)
            raise
        try:
            self._self_on_chunk(chunk)
        except Exception:  # noqa: BLE001
            pass
        return chunk

    def __enter__(self) -> "TracedStream":
        enter = getattr(self.__wrapped__, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            leave = getattr(self.__wrapped__, "__exit__", None)
            return leave(*exc_info) if leave is not None else None
        finally:
            # An exception raised in the with-block is the call's failure: the span ends with it.
            self._end(error=_raised(exc_info), incomplete=True)

    def close(self) -> Any:
        try:
            close = getattr(self.__wrapped__, "close", None)
            return close() if close is not None else None
        finally:
            self._end(incomplete=True)

    def __del__(self) -> None:
        try:
            self._end(incomplete=True)
        except Exception:  # noqa: BLE001
            pass


class TracedAsyncStream(_StreamState, ObjectProxy):
    """Async stream."""

    def __init__(self, stream: Any, on_chunk: Callable[[Any], None], finish: Finish) -> None:
        super().__init__(stream)
        self._self_on_chunk = on_chunk
        self._self_finish = finish
        self._self_done = False
        self._self_iterator = None

    def __aiter__(self) -> "TracedAsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._self_iterator is None:
            self._self_iterator = self.__wrapped__.__aiter__()
        try:
            chunk = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            self._end()
            raise
        except BaseException as exc:
            self._end(error=exc)
            raise
        try:
            self._self_on_chunk(chunk)
        except Exception:  # noqa: BLE001
            pass
        return chunk

    async def __aenter__(self) -> "TracedAsyncStream":
        enter = getattr(self.__wrapped__, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            leave = getattr(self.__wrapped__, "__aexit__", None)
            return await leave(*exc_info) if leave is not None else None
        finally:
            self._end(error=_raised(exc_info), incomplete=True)

    async def aclose(self) -> Any:
        try:
            close = getattr(self.__wrapped__, "aclose", None) or getattr(self.__wrapped__, "close", None)
            if close is None:
                return None
            result = close()
            return await result if hasattr(result, "__await__") else result
        finally:
            self._end(incomplete=True)

    async def close(self) -> Any:
        return await self.aclose()

    def __del__(self) -> None:
        try:
            self._end(incomplete=True)
        except Exception:  # noqa: BLE001
            pass
