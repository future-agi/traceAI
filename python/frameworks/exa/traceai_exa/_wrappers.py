"""wrapt wrappers for the supported Exa client methods."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Mapping, Optional, Sequence

from opentelemetry.trace import Span, Status, StatusCode, Tracer

_FI_SPAN_KIND = "fi.span.kind"
_INPUT_VALUE = "input.value"
_RETRIEVAL_QUERY = "fi.retrieval.query"
_RETRIEVAL_DOCUMENT_COUNT = "fi.retrieval.document_count"
_RETRIEVAL_URL_COUNT = "fi.retrieval.url_count"
_RETRIEVAL_URLS = "fi.retrieval.urls"
_RETRIEVER = "RETRIEVER"
_REDACTED = "[redacted]"
_MAX_QUERY_BYTES = 1024
_MAX_CAPTURED_URLS = 20


def _redact(value: str, api_key: Optional[str]) -> str:
    return value.replace(api_key, _REDACTED) if api_key else value


def _query(args: Sequence[Any], kwargs: Mapping[str, Any]) -> Optional[str]:
    """Return the search or answer query argument, never client configuration."""
    value = kwargs.get("query")
    if value is None and args:
        value = args[0]
    return None if value is None else str(value)


def _requested_urls(args: Sequence[Any], kwargs: Mapping[str, Any]) -> Optional[list]:
    """Return get_contents' requested URLs, or None when the shape is unknown.

    exa-py accepts one URL, a list of URLs, or a list of Result objects. Only a
    Result's ``url`` is read, so text and highlights cannot reach a span.
    """
    value = kwargs["urls"] if "urls" in kwargs else (args[0] if args else None)
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item if isinstance(item, str) else getattr(item, "url", None) for item in value]
    return None


def _request_attributes(
    instance: Any,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    contents: bool,
    capture_urls: bool,
) -> dict:
    attributes: dict = {_FI_SPAN_KIND: _RETRIEVER}
    api_key = _api_key(instance)
    if contents:
        # get_contents: a URL count by default. URLs can carry tokens or
        # personal data, so they are recorded only with capture_urls=True.
        urls = _requested_urls(args, kwargs)
        if urls is not None:
            attributes[_RETRIEVAL_URL_COUNT] = len(urls)
            if capture_urls:
                attributes[_RETRIEVAL_URLS] = [
                    _cap(_redact(url, api_key))
                    for url in urls[:_MAX_CAPTURED_URLS]
                    if isinstance(url, str)
                ]
        return attributes

    query = _query(args, kwargs)
    if query is not None:
        query = _cap(_redact(query, api_key))
        attributes[_RETRIEVAL_QUERY] = query
        # The backend input panel reads input.value; keep both keys.
        attributes[_INPUT_VALUE] = query
    return attributes


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
    def __init__(
        self,
        tracer: Tracer,
        span_name: str,
        *,
        contents: bool = False,
        capture_urls: bool = False,
    ) -> None:
        self._tracer = tracer
        self._span_name = span_name
        self._contents = contents
        self._capture_urls = capture_urls

    def _start_span(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> Span:
        try:
            attributes = _request_attributes(
                instance, args, kwargs, self._contents, self._capture_urls
            )
        except Exception:  # an attribute must never break the user's call
            attributes = {_FI_SPAN_KIND: _RETRIEVER}
        return self._tracer.start_span(self._span_name, attributes=attributes)

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
