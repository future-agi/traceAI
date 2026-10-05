"""wrapt wrappers for TavilyClient / AsyncTavilyClient search and extract."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Sequence

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

# extract(urls, include_images, extract_depth, format, timeout,
#         include_favicon, include_usage, query, ...) in tavily-python 0.8.4.
_EXTRACT_QUERY_POSITION = 7


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

    def _start(self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Span:
        attributes: Dict[str, Any] = {_TOOL_NAME: self._span_name}
        query = _query(self._method, args, kwargs)
        if query is not None:
            attributes[_INPUT_VALUE] = query
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
        span = self._start(instance, args, kwargs)
        token = _attach(span)
        try:
            result = wrapped(*args, **kwargs)
        except BaseException:
            span.end()
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
        span = self._start(instance, args, kwargs)
        token = _attach(span)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException:
            span.end()
            raise
        finally:
            _detach(token)
        self._finish_ok(span, result)
        return result
