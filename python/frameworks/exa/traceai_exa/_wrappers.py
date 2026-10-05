"""wrapt wrappers for the supported Exa client methods."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Mapping, Optional, Sequence

from opentelemetry.trace import Span, Status, StatusCode, Tracer

_FI_SPAN_KIND = "fi.span.kind"
_RETRIEVAL_QUERY = "fi.retrieval.query"
_RETRIEVAL_DOCUMENT_COUNT = "fi.retrieval.document_count"
_RETRIEVER = "RETRIEVER"
_MAX_QUERY_BYTES = 1024


def _query_from_call(
    instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> str:
    """Return only the user query or URL input, never client configuration."""
    value = kwargs.get("query")
    if value is None and args:
        value = args[0]
    if value is None:
        value = kwargs.get("urls", kwargs.get("ids", ""))

    if isinstance(value, str):
        query = value
    elif isinstance(value, (list, tuple)):
        # get_contents also accepts Result objects. Use only their URLs so
        # content fields such as text and highlights cannot be traced.
        query = ",".join(
            item if isinstance(item, str) else str(getattr(item, "url", ""))
            for item in value
        )
    else:
        query = str(getattr(value, "url", value or ""))

    api_key = _api_key(instance)
    if api_key:
        query = query.replace(api_key, "[redacted]")
    return _cap(query)


def _cap(value: str, limit: int = _MAX_QUERY_BYTES) -> str:
    """Return the longest whole-character prefix of at most ``limit`` UTF-8 bytes."""
    # A cut inside a multi-byte character leaves an invalid tail; "ignore"
    # drops it. "replace" keeps a lone surrogate from raising in user code.
    return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _api_key(instance: Any) -> Optional[str]:
    """Return the client's Exa key so it can be removed from span values.

    exa-py keeps the key only in ``headers["x-api-key"]``; ``api_key`` is a
    fallback for clients that expose it as an attribute.
    """
    key = None
    headers = getattr(instance, "headers", None)
    if isinstance(headers, Mapping):
        key = headers.get("x-api-key")
    if not key:
        key = getattr(instance, "api_key", None)
    return key if isinstance(key, str) and key else None


def _document_count(result: Any) -> int:
    """Count returned documents without inspecting their content."""
    results = getattr(result, "results", None)
    if results is None:
        return len(result) if isinstance(result, (list, tuple, set)) else 0
    try:
        return len(results)
    except TypeError:
        return 0


class _BaseWrapper:
    def __init__(self, tracer: Tracer, span_name: str) -> None:
        self._tracer = tracer
        self._span_name = span_name

    def _start_span(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> Span:
        return self._tracer.start_span(
            self._span_name,
            attributes={
                _FI_SPAN_KIND: _RETRIEVER,
                _RETRIEVAL_QUERY: _query_from_call(instance, args, kwargs),
            },
        )

    @staticmethod
    def _finish_ok(span: Span, result: Any = None) -> None:
        span.set_attribute(_RETRIEVAL_DOCUMENT_COUNT, _document_count(result))
        span.set_status(Status(StatusCode.OK))
        span.end()

    @staticmethod
    def _finish_error(span: Span, error: BaseException) -> None:
        span.set_attribute(_RETRIEVAL_DOCUMENT_COUNT, 0)
        span.record_exception(error)
        span.set_status(
            Status(StatusCode.ERROR, "{0}: {1}".format(type(error).__name__, error))
        )
        span.end()


class OperationWrapper(_BaseWrapper):
    """Trace a synchronous Exa operation."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        span = self._start_span(instance, args, kwargs)
        try:
            result = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        self._finish_ok(span, result)
        return result


class AsyncOperationWrapper(_BaseWrapper):
    """Trace an asynchronous Exa operation."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        span = self._start_span(instance, args, kwargs)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        self._finish_ok(span, result)
        return result


class _StreamIterator:
    """Finish a synchronous stream span when its iterator completes or closes."""

    def __init__(self, response: Any, span: Span) -> None:
        self._response = response
        self._iterator = iter(response)
        self._span = span
        self._finished = False

    def __iter__(self) -> "_StreamIterator":
        return self

    def __next__(self) -> Any:
        try:
            return next(self._iterator)
        except StopIteration:
            self._finish_ok()
            raise
        except BaseException as error:
            self._finish_error(error)
            raise

    def close(self) -> None:
        try:
            close = getattr(self._iterator, "close", None)
            if close is not None:
                close()
            response_close = getattr(self._response, "close", None)
            if response_close is not None and self._iterator is not self._response:
                response_close()
        finally:
            self._finish_error(RuntimeError("Exa stream cancelled"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __del__(self) -> None:
        if not getattr(self, "_finished", True):
            self._finish_error(RuntimeError("Exa stream cancelled"))

    def _finish_ok(self) -> None:
        if not self._finished:
            self._finished = True
            _BaseWrapper._finish_ok(self._span)

    def _finish_error(self, error: BaseException) -> None:
        if not self._finished:
            self._finished = True
            _BaseWrapper._finish_error(self._span, error)


class StreamWrapper(_BaseWrapper):
    """Trace a synchronous Exa stream for its full iterator lifetime."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        span = self._start_span(instance, args, kwargs)
        try:
            response = wrapped(*args, **kwargs)
            return _StreamIterator(response, span)
        except BaseException as error:
            self._finish_error(span, error)
            raise


class _AsyncStreamIterator:
    """Finish an async stream span when its iterator completes or closes."""

    def __init__(self, response: Any, span: Span) -> None:
        self._response = response
        self._iterator = response.__aiter__()
        self._span = span
        self._finished = False

    def __aiter__(self) -> "_AsyncStreamIterator":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            self._finish_ok()
            raise
        except BaseException as error:
            self._finish_error(error)
            raise

    async def aclose(self) -> None:
        try:
            close = getattr(self._iterator, "aclose", None)
            if close is not None:
                await close()
            response_close = getattr(self._response, "aclose", None)
            if response_close is not None and self._iterator is not self._response:
                await response_close()
        finally:
            self._finish_error(RuntimeError("Exa stream cancelled"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __del__(self) -> None:
        if not getattr(self, "_finished", True):
            self._finish_error(RuntimeError("Exa stream cancelled"))

    def _finish_ok(self) -> None:
        if not self._finished:
            self._finished = True
            _BaseWrapper._finish_ok(self._span)

    def _finish_error(self, error: BaseException) -> None:
        if not self._finished:
            self._finished = True
            _BaseWrapper._finish_error(self._span, error)


class AsyncStreamWrapper(_BaseWrapper):
    """Trace an asynchronous Exa stream for its full iterator lifetime."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        span = self._start_span(instance, args, kwargs)
        try:
            response = wrapped(*args, **kwargs)
            if inspect.isawaitable(response):
                response = await response
            return _AsyncStreamIterator(response, span)
        except BaseException as error:
            self._finish_error(span, error)
            raise
