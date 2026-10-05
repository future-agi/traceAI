"""wrapt wrappers for TavilyClient / AsyncTavilyClient search and extract."""

from __future__ import annotations

import asyncio
import concurrent.futures
import traceback
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode

_SPAN_KIND = "gen_ai.span.kind"
_TOOL = "TOOL"
_TOOL_NAME = "gen_ai.tool.name"
_INPUT_VALUE = "input.value"
_URL_COUNT = "tavily.url_count"
_RESULT_COUNT = "tavily.result_count"
_FAILED_RESULT_COUNT = "tavily.failed_result_count"
_CANCELLED = "tavily.cancelled"
_CANCELLATION_ERRORS = (asyncio.CancelledError, concurrent.futures.CancelledError)
_REDACTED = "[redacted]"
_MAX_TEXT_BYTES = 1024

# extract(urls, include_images, extract_depth, format, timeout,
#         include_favicon, include_usage, query, ...) in tavily-python 0.8.4.
_EXTRACT_QUERY_POSITION = 7


def _bearer_token(value: Any) -> Optional[str]:
    """The key in an ``Authorization`` header value (``Bearer <key>``)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value[:7].lower() == "bearer ":
        value = value[7:].strip()
    return value or None


def _api_keys(instance: Any) -> List[str]:
    """Every Tavily key the client holds, longest first.

    tavily-python 0.8.4 keeps it in ``api_key``, ``headers["Authorization"]``
    and the requests session's headers (TavilyClient), or only in the httpx
    client's headers (AsyncTavilyClient, ``_client``). A caller's own session
    or httpx client can carry a key that ``api_key`` does not.
    """
    candidates: List[Any] = [getattr(instance, "api_key", None)]
    for holder in (instance, getattr(instance, "session", None), getattr(instance, "_client", None)):
        headers = getattr(holder, "headers", None) if holder is not None else None
        if headers is not None:
            candidates.append(_bearer_token(headers.get("Authorization")))
    keys: List[str] = []
    for key in candidates:
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)
    return sorted(keys, key=len, reverse=True)


def _redact(text: str, keys: Sequence[str]) -> str:
    for key in keys:
        text = text.replace(key, _REDACTED)
    return text


def _cap(text: str, limit: int = _MAX_TEXT_BYTES) -> str:
    """The longest whole-character prefix of at most ``limit`` UTF-8 bytes."""
    # A cut inside a multi-byte character leaves an invalid tail that "ignore"
    # drops; "replace" keeps a lone surrogate from raising.
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _query(method: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Optional[str]:
    """The search query, or extract's optional rerank query."""
    if "query" in kwargs:
        value = kwargs["query"]
    elif method == "search":
        value = args[0] if args else None
    else:
        value = args[_EXTRACT_QUERY_POSITION] if len(args) > _EXTRACT_QUERY_POSITION else None
    return None if value is None else str(value)


def _url_count(args: Sequence[Any], kwargs: Mapping[str, Any]) -> Optional[int]:
    value = kwargs["urls"] if "urls" in kwargs else (args[0] if args else None)
    if isinstance(value, str):
        return 1
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def _count(result: Any, key: str) -> Optional[int]:
    """Length of a list in the response; None (not 0) when it is not a list."""
    value = result.get(key) if isinstance(result, Mapping) else None
    return len(value) if isinstance(value, list) else None


class _Operation:
    """Span bookkeeping shared by the sync and async wrappers."""

    def __init__(self, tracer: Any, method: str) -> None:
        self._tracer = tracer
        self._method = method
        self._span_name = "tavily.{0}".format(method)

    def _start(self, keys: Sequence[str], args: Sequence[Any], kwargs: Mapping[str, Any]) -> Span:
        attributes: Dict[str, Any] = {_TOOL_NAME: self._span_name}
        query = _query(self._method, args, kwargs)
        if query is not None:
            # Redact first, then cap, so a key cut at the limit leaves no prefix.
            attributes[_INPUT_VALUE] = _cap(_redact(query, keys))
        if self._method == "extract":
            urls = _url_count(args, kwargs)
            if urls is not None:
                attributes[_URL_COUNT] = urls
        span = self._tracer.start_span(self._span_name, attributes=attributes)
        span.set_attribute(_SPAN_KIND, _TOOL)
        return span

    def _finish_ok(self, span: Span, result: Any) -> None:
        count = _count(result, "results")
        if count is not None:
            span.set_attribute(_RESULT_COUNT, count)
        if self._method == "extract":
            failed = _count(result, "failed_results")
            if failed is not None:
                span.set_attribute(_FAILED_RESULT_COUNT, failed)
        span.set_status(Status(StatusCode.OK))
        span.end()

    @staticmethod
    def _finish_error(span: Span, error: BaseException, keys: Sequence[str]) -> None:
        if isinstance(error, _CANCELLATION_ERRORS):
            # Cancellation is not a failure of the call: no exception event.
            span.set_attribute(_CANCELLED, True)
            span.set_status(Status(StatusCode.ERROR, "cancelled"))
        else:
            message = _redact(str(error), keys)
            # The SDK would copy str(error) into the event; an error body can
            # repeat the query, so the key is removed there too.
            event: Optional[Dict[str, Any]] = None
            if keys:
                stacktrace = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
                event = {
                    "exception.message": message,
                    "exception.stacktrace": _redact(stacktrace, keys),
                }
            span.record_exception(error, attributes=event)
            span.set_status(
                Status(StatusCode.ERROR, _cap("{0}: {1}".format(type(error).__name__, message)))
            )
        span.end()


def _attach(span: Span) -> Any:
    return context_api.attach(trace_api.set_span_in_context(span))


def _detach(token: Any) -> None:
    context_api.detach(token)


class SyncWrapper(_Operation):
    """Trace a TavilyClient call."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        keys = _api_keys(instance)
        span = self._start(keys, args, kwargs)
        token = _attach(span)
        try:
            result = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error, keys)
            raise
        finally:
            _detach(token)
        self._finish_ok(span, result)
        return result


class AsyncWrapper(_Operation):
    """Trace an AsyncTavilyClient call."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        keys = _api_keys(instance)
        span = self._start(keys, args, kwargs)
        token = _attach(span)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error, keys)
            raise
        finally:
            _detach(token)
        self._finish_ok(span, result)
        return result
