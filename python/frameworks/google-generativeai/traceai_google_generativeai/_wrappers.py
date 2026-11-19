import logging
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

import opentelemetry.context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN

from fi_instrumentation import get_attributes_from_context
from fi_instrumentation.fi_types import SpanAttributes
from traceai_google_generativeai import _attributes as attrs

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_TTFT = SpanAttributes.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN


def _error_status(exception: BaseException) -> trace_api.Status:
    return trace_api.Status(trace_api.StatusCode.ERROR, f"{type(exception).__name__}: {exception}")


class _Call:
    """One traced call: owns the span and guarantees it is ended exactly once."""

    def __init__(self, tracer: trace_api.Tracer, name: str, attributes: Callable[[], Dict[str, Any]]) -> None:
        self.finished = False
        self.span: trace_api.Span = INVALID_SPAN
        try:
            self.span = tracer.start_span(name=name, attributes=attributes())
            for key, value in get_attributes_from_context():
                self.span.set_attribute(key, value)
        except Exception:
            logger.debug("failed to start span", exc_info=True)
            self.finished = True

    @contextmanager
    def current(self) -> Iterator[None]:
        with trace_api.use_span(
            self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        ):
            yield

    def finish(
        self,
        attributes: Optional[Mapping[str, Any]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            for key, value in (attributes or {}).items():
                if value is not None:
                    self.span.set_attribute(key, value)
            if error is not None:
                self.span.record_exception(error)
                self.span.set_attribute("error.type", type(error).__name__)
                self.span.set_status(_error_status(error))
            else:
                self.span.set_status(trace_api.Status(trace_api.StatusCode.OK))
        except Exception:
            logger.debug("failed to record span result", exc_info=True)
        try:
            self.span.end()
        except Exception:
            logger.debug("failed to end span", exc_info=True)

    def finish_with_response(self, response: Any, error: Optional[BaseException] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            attributes = attrs.response_attributes(response)
        except Exception:
            logger.debug("failed to read response", exc_info=True)
            attributes = {}
        attributes.update(extra or {})
        self.finish(attributes, error)


def _suppressed() -> bool:
    return bool(context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY))


def _abandon(call: _Call) -> None:
    call.finish({"gen_ai.response.incomplete": True})


class _SyncStream:
    """Iterator standing in for the SDK's chunk iterator; ends the span when the stream ends."""

    def __init__(self, inner: Any, call: _Call, owner: Any) -> None:
        self._inner = inner
        self._call = call
        self._owner = weakref.ref(owner)

    def __iter__(self) -> "_SyncStream":
        return self

    def __next__(self) -> Any:
        try:
            return next(self._inner)
        except StopIteration:
            self._call.finish_with_response(self._owner())
            raise
        except BaseException as exc:
            self._call.finish_with_response(self._owner(), error=exc)
            raise


class _AsyncStream:
    def __init__(self, inner: Any, call: _Call, owner: Any) -> None:
        self._inner = inner
        self._call = call
        self._owner = weakref.ref(owner)

    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration:
            self._call.finish_with_response(self._owner())
            raise
        except BaseException as exc:
            self._call.finish_with_response(self._owner(), error=exc)
            raise


def _follow_stream(call: _Call, response: Any, stream_class: type, started: float) -> None:
    """Hand the span's end to the response's own chunk iterator."""
    ttft = {_TTFT: round((time.perf_counter() - started) * 1000, 3)}
    inner = getattr(response, "_iterator", None)
    if inner is None or not hasattr(response, "_result") or getattr(response, "_done", False):
        call.finish_with_response(response, extra=ttft)
        return
    try:
        call.span.set_attribute(_TTFT, ttft[_TTFT])
    except Exception:
        logger.debug("failed to record time to first token", exc_info=True)
    response._iterator = stream_class(inner, call, response)
    weakref.finalize(response, _abandon, call)


class _Wrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer


class GenerateContent(_Wrapper):
    def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        if _suppressed():
            return wrapped(*args, **kwargs)
        arguments = attrs.bind(wrapped, args, kwargs)
        call = _Call(self._tracer, "GenerateContent", lambda: attrs.request_attributes(instance, arguments))
        started = time.perf_counter()
        try:
            with call.current():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            call.finish(error=exc)
            raise
        try:
            if arguments.get("stream"):
                _follow_stream(call, response, _SyncStream, started)
            else:
                call.finish_with_response(response)
        except Exception:
            logger.debug("failed to finalize response", exc_info=True)
            call.finish()
        return response


class AsyncGenerateContent(_Wrapper):
    async def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        if _suppressed():
            return await wrapped(*args, **kwargs)
        arguments = attrs.bind(wrapped, args, kwargs)
        call = _Call(self._tracer, "AsyncGenerateContent", lambda: attrs.request_attributes(instance, arguments))
        started = time.perf_counter()
        try:
            with call.current():
                response = await wrapped(*args, **kwargs)
        except BaseException as exc:
            call.finish(error=exc)
            raise
        try:
            if arguments.get("stream"):
                _follow_stream(call, response, _AsyncStream, started)
            else:
                call.finish_with_response(response)
        except Exception:
            logger.debug("failed to finalize response", exc_info=True)
            call.finish()
        return response


class EmbedContent(_Wrapper):
    def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        if _suppressed():
            return wrapped(*args, **kwargs)
        arguments = attrs.bind(wrapped, args, kwargs)
        call = _Call(self._tracer, "EmbedContent", lambda: attrs.embedding_request_attributes(arguments))
        try:
            with call.current():
                result = wrapped(*args, **kwargs)
        except BaseException as exc:
            call.finish(error=exc)
            raise
        call.finish(_safe_embedding(result))
        return result


class AsyncEmbedContent(_Wrapper):
    async def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        if _suppressed():
            return await wrapped(*args, **kwargs)
        arguments = attrs.bind(wrapped, args, kwargs)
        call = _Call(self._tracer, "AsyncEmbedContent", lambda: attrs.embedding_request_attributes(arguments))
        try:
            with call.current():
                result = await wrapped(*args, **kwargs)
        except BaseException as exc:
            call.finish(error=exc)
            raise
        call.finish(_safe_embedding(result))
        return result


def _safe_embedding(result: Any) -> Dict[str, Any]:
    try:
        return attrs.embedding_response_attributes(result)
    except Exception:
        return {}
