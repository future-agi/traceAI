"""Observe the lifetime of returned LLM generators, including old cores.

Core 0.10.43 emits no event on generator close/failure; even current cores emit
nothing when a generator is closed before its first iteration. Span exit cannot
replace a result, so wrap the public streaming methods as well as listening to
the dispatcher's events. The proxy preserves send/throw and async equivalents.
"""

import inspect
import logging
from contextvars import ContextVar
from threading import RLock
from typing import Any, AsyncGenerator, Callable, Dict, Generator, List, Optional, Tuple

from wrapt import FunctionWrapper, ObjectProxy

logger = logging.getLogger(__name__)
_stream_capture: ContextVar[Optional[List[Any]]] = ContextVar(
    "traceai_llamaindex_stream", default=None
)
# Async synthesizers can create their model stream only on first consumption,
# after the dispatcher's call context has gone away.
_response_span: ContextVar[Optional[Any]] = ContextVar(
    "traceai_llamaindex_response_span", default=None
)


def _finish_spans(spans: List[Any], error: Optional[BaseException] = None) -> None:
    from ._handler import _StreamingStatus

    for span in spans:
        try:
            span.finish(error)
            span.notify_parent(_StreamingStatus.FINISHED, error)
        except Exception:
            logger.exception("Failed to finish streaming span")


class _Stream(ObjectProxy):
    def __init__(self, stream: Any, spans: List[Any]) -> None:
        super().__init__(stream)
        self._self_spans = spans

    def _finish(self, error: Optional[BaseException] = None) -> None:
        spans, self._self_spans = self._self_spans, []
        _finish_spans(spans, error)

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def send(self, value: Any) -> Any:
        return self._advance(self.__wrapped__.send, value)

    def throw(self, *args: Any) -> Any:
        return self._advance(self.__wrapped__.throw, *args)

    def _advance(self, method: Callable[..., Any], *args: Any) -> Any:
        try:
            return method(*args)
        except StopIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise

    def close(self) -> Any:
        error: BaseException = GeneratorExit()
        try:
            return self.__wrapped__.close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish(error)


class _AsyncStream(_Stream):
    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        return await self.asend(None)

    async def asend(self, value: Any) -> Any:
        return await self._advance_async(self.__wrapped__.asend, value)

    async def athrow(self, *args: Any) -> Any:
        return await self._advance_async(self.__wrapped__.athrow, *args)

    async def _advance_async(self, method: Callable[..., Any], *args: Any) -> Any:
        try:
            return await method(*args)
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise

    async def aclose(self) -> Any:
        error: BaseException = GeneratorExit()
        try:
            return await self.__wrapped__.aclose()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish(error)


class _ResponseStream(_Stream):
    """Observe a response container's tokens without reading ahead.

    A query and its synthesizer can return the same container. They share this
    observer, so every span ends at the consumer's terminal operation, not at a
    nested LLM's end event (which can precede the final synthesized token).
    """

    def __init__(
        self, stream: Any, spans: List[Any], finish_children: Callable[..., None]
    ) -> None:
        super().__init__(stream, spans)
        self._self_finish_children = finish_children

    def _record_chunk(self, chunk: Any) -> None:
        for span in self._self_spans:
            try:
                span.process_response_chunk(chunk)
            except Exception:
                logger.exception("Failed to record response token")

    def _advance(self, method: Callable[..., Any], *args: Any) -> Any:
        token = _response_span.set(self._self_spans[0] if self._self_spans else None)
        try:
            chunk = super()._advance(method, *args)
            self._record_chunk(chunk)
            return chunk
        finally:
            _response_span.reset(token)

    def _finish(self, error: Optional[BaseException] = None) -> None:
        for span in self._self_spans:
            try:
                self._self_finish_children(span, error)
            except Exception:
                logger.exception("Failed to finish response child spans")
        super()._finish(error)


class _AsyncResponseStream(_ResponseStream, _AsyncStream):
    async def _advance_async(self, method: Callable[..., Any], *args: Any) -> Any:
        token = _response_span.set(self._self_spans[0] if self._self_spans else None)
        try:
            chunk = await super()._advance_async(method, *args)
            self._record_chunk(chunk)
            return chunk
        finally:
            _response_span.reset(token)


def _wrap_result(result: Any, spans: List[Any]) -> Any:
    if spans:
        if isinstance(result, Generator):
            return _Stream(result, spans)
        if isinstance(result, AsyncGenerator):
            return _AsyncStream(result, spans)
    return result


_MISSING = object()
_STREAM_METHODS = ("stream_chat", "stream_complete", "astream_chat", "astream_complete")


class StreamInstrumentation:
    def __init__(self) -> None:
        self._patches: Dict[Tuple[type, str], Any] = {}
        self._lock = RLock()
        self._enabled = True

    def install(self) -> None:
        from llama_index.core.base.llms.base import BaseLLM
        from llama_index.core.multi_modal_llms import MultiModalLLM

        seen = set()

        def visit(cls: type) -> None:
            if cls in seen:
                return
            seen.add(cls)
            self._patch_class(cls)
            for child in cls.__subclasses__():
                visit(child)

        for base in (BaseLLM, MultiModalLLM):
            visit(base)
            # Providers imported after instrument() also need their first call
            # observed. Pydantic models run this inherited initializer.
            self._patch(base, "__init__", self._initialize)

    def _patch_class(self, cls: type) -> None:
        for base in reversed(cls.__mro__):
            for name in _STREAM_METHODS:
                attr = base.__dict__.get(name)
                if attr is not None and not getattr(attr, "__isabstractmethod__", False):
                    wrapper = (
                        self._call_async if inspect.iscoroutinefunction(attr) else self._call
                    )
                    self._patch(base, name, wrapper)

    def _patch(self, cls: type, name: str, wrapper: Callable[..., Any]) -> None:
        with self._lock:
            if not self._enabled or (cls, name) in self._patches:
                return
            attr = getattr(cls, name)
            self._patches[cls, name] = cls.__dict__.get(name, _MISSING)
            setattr(cls, name, FunctionWrapper(attr, wrapper))

    def _initialize(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        if self._enabled:
            self._patch_class(type(instance))
        return wrapped(*args, **kwargs)

    def _call(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        if not self._enabled:
            return wrapped(*args, **kwargs)
        spans = []
        parent_capture = _stream_capture.get()
        token = _stream_capture.set(spans)
        try:
            result = wrapped(*args, **kwargs)
        except BaseException as error:
            _finish_spans(spans, error)
            raise
        finally:
            _stream_capture.reset(token)
            if parent_capture is not None:
                parent_capture.extend(spans)
        return _wrap_result(result, spans)

    async def _call_async(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        if not self._enabled:
            return await wrapped(*args, **kwargs)
        spans = []
        parent_capture = _stream_capture.get()
        token = _stream_capture.set(spans)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as error:
            _finish_spans(spans, error)
            raise
        finally:
            _stream_capture.reset(token)
            if parent_capture is not None:
                parent_capture.extend(spans)
        return _wrap_result(result, spans)

    def uninstall(self) -> None:
        self._enabled = False
        with self._lock:
            for (cls, name), original in reversed(list(self._patches.items())):
                attr = cls.__dict__.get(name)
                if (
                    not isinstance(attr, FunctionWrapper)
                    or getattr(attr._self_wrapper, "__self__", None) is not self
                ):
                    continue  # Do not overwrite another instrumentor's patch.
                if original is _MISSING:
                    delattr(cls, name)
                else:
                    setattr(cls, name, original)
            self._patches.clear()
