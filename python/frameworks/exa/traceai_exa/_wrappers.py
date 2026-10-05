"""wrapt wrappers for the supported Exa client methods."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Mapping, Optional, Sequence

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode, Tracer

_FI_SPAN_KIND = "fi.span.kind"
_INPUT_VALUE = "input.value"
_RETRIEVAL_QUERY = "fi.retrieval.query"
_RETRIEVAL_DOCUMENT_COUNT = "fi.retrieval.document_count"
_RETRIEVAL_URL_COUNT = "fi.retrieval.url_count"
_RETRIEVAL_URLS = "fi.retrieval.urls"
_CANCELLED = "exa.cancelled"
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


def _document_count(result: Any) -> Optional[int]:
    """Count returned documents without inspecting their content.

    SearchResponse (search, get_contents) has ``results``; AnswerResponse has
    ``citations``. Anything else is an unknown count: None, never 0.
    """
    for name in ("results", "citations"):
        documents = getattr(result, name, None)
        if documents is not None:
            try:
                return len(documents)
            except TypeError:
                return None
    return None


def _chunk_citations(chunk: Any) -> int:
    """Citations carried by one StreamChunk (stream_search / stream_answer)."""
    try:
        return len(getattr(chunk, "citations", None) or ())
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
    def _finish_ok(span: Span, document_count: Optional[int]) -> None:
        if document_count is not None:
            span.set_attribute(_RETRIEVAL_DOCUMENT_COUNT, document_count)
        span.set_status(Status(StatusCode.OK))
        span.end()

    @staticmethod
    def _finish_error(span: Span, error: BaseException) -> None:
        # No document count: nothing was returned, so the count is unknown.
        span.record_exception(error)
        span.set_status(
            Status(StatusCode.ERROR, "{0}: {1}".format(type(error).__name__, error))
        )
        span.end()

    @staticmethod
    def _finish_cancelled(span: Span) -> None:
        # Cancellation is not an exception: no event and no document count.
        span.set_attribute(_CANCELLED, True)
        span.set_status(Status(StatusCode.ERROR, "cancelled"))
        span.end()


def _current(span: Span) -> Any:
    """Make ``span`` current for the vendor call so HTTP client spans nest under it.

    The span is ended by the wrapper, not on exit, and errors are recorded once
    by ``_finish_error``.
    """
    return trace_api.use_span(
        span, end_on_exit=False, record_exception=False, set_status_on_exception=False
    )


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
            with _current(span):
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        self._finish_ok(span, _document_count(result))
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
            with _current(span):
                result = await wrapped(*args, **kwargs)
        except asyncio.CancelledError:
            self._finish_cancelled(span)
            raise
        except BaseException as error:
            self._finish_error(span, error)
            raise
        self._finish_ok(span, _document_count(result))
        return result


class _StreamState:
    """Ends one stream span exactly once; counts citations as chunks arrive."""

    def __init__(self, span: Span) -> None:
        self.span = span
        self.finished = False
        # Citations accumulate over chunks; the total is known only at the end.
        self.citations = 0

    def observe(self, chunk: Any) -> None:
        self.citations += _chunk_citations(chunk)

    def ok(self) -> None:
        if not self.finished:
            self.finished = True
            _BaseWrapper._finish_ok(self.span, self.citations)

    def error(self, error: BaseException) -> None:
        if not self.finished:
            self.finished = True
            _BaseWrapper._finish_error(self.span, error)

    def cancelled(self) -> None:
        if not self.finished:
            self.finished = True
            _BaseWrapper._finish_cancelled(self.span)


def _state_of(proxy: Any) -> Optional[_StreamState]:
    # A proxy whose __init__ failed has no state; __del__ must not raise.
    try:
        return proxy._self_state
    except Exception:
        return None


class _TracedStream(wrapt.ObjectProxy):  # type: ignore[misc]
    """A StreamSearchResponse/StreamAnswerResponse that ends its span.

    An ObjectProxy, so ``isinstance`` against the vendor class still holds.
    """

    def __init__(self, response: Any, span: Span) -> None:
        super().__init__(response)
        self._self_state = _StreamState(span)
        self._self_iterator = None

    def __iter__(self) -> "_TracedStream":
        return self

    def __next__(self) -> Any:
        state = self._self_state
        try:
            if self._self_iterator is None:
                self._self_iterator = iter(self.__wrapped__)
            chunk = next(self._self_iterator)
        except StopIteration:
            state.ok()
            raise
        except BaseException as error:
            state.error(error)
            raise
        state.observe(chunk)
        return chunk

    def close(self) -> None:
        """End the span as cancelled (no-op after completion), then close."""
        self._self_state.cancelled()
        try:
            close = getattr(self._self_iterator, "close", None)
            if close is not None:
                close()
        finally:
            self.__wrapped__.close()

    def __del__(self) -> None:
        state = _state_of(self)
        if state is not None:
            state.cancelled()


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
            with _current(span):
                response = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        return _TracedStream(response, span)


class _TracedAsyncStream(wrapt.ObjectProxy):  # type: ignore[misc]
    """An AsyncStreamSearchResponse/AsyncStreamAnswerResponse that ends its span."""

    def __init__(self, response: Any, span: Span) -> None:
        super().__init__(response)
        self._self_state = _StreamState(span)
        self._self_iterator = None

    def __aiter__(self) -> "_TracedAsyncStream":
        return self

    async def __anext__(self) -> Any:
        state = self._self_state
        try:
            if self._self_iterator is None:
                self._self_iterator = self.__wrapped__.__aiter__()
            chunk = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            state.ok()
            raise
        except asyncio.CancelledError:
            state.cancelled()
            raise
        except BaseException as error:
            state.error(error)
            raise
        state.observe(chunk)
        return chunk

    def close(self) -> None:
        """End the span as cancelled, then delegate to the vendor's close().

        exa-py 2.25.0's async close() calls httpx's sync close, which raises on
        an async response; that outcome is the vendor's and is not changed.
        """
        self._self_state.cancelled()
        self.__wrapped__.close()

    async def aclose(self) -> None:
        """End the span as cancelled, then release the HTTP response."""
        self._self_state.cancelled()
        try:
            close = getattr(self._self_iterator, "aclose", None)
            if close is not None:
                await close()
        finally:
            # exa-py 2.25.0 has no aclose(); its httpx response does.
            response_close = getattr(self.__wrapped__, "aclose", None)
            if response_close is None:
                raw_response = getattr(self.__wrapped__, "_raw_response", None)
                response_close = getattr(raw_response, "aclose", None)
            if response_close is not None:
                await response_close()

    def __del__(self) -> None:
        state = _state_of(self)
        if state is not None:
            state.cancelled()


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
            with _current(span):
                response = wrapped(*args, **kwargs)
                if inspect.isawaitable(response):
                    response = await response
        except asyncio.CancelledError:
            self._finish_cancelled(span)
            raise
        except BaseException as error:
            self._finish_error(span, error)
            raise
        return _TracedAsyncStream(response, span)
