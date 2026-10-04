"""wrapt wrappers for the supported Firecrawl v2 client methods."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from opentelemetry.trace import Span, Status, StatusCode, Tracer

_FI_SPAN_KIND = "fi.span.kind"
_TOOL = "TOOL"
_RETRIEVAL_QUERY = "fi.retrieval.query"
_RETRIEVAL_DOCUMENT_COUNT = "fi.retrieval.document_count"
_JOB_ID = "firecrawl.job_id"
_LIMIT = "firecrawl.limit"
_PAGE_COUNT = "firecrawl.page_count"
_CREDITS_USED = "firecrawl.credits_used"
_MAX_QUERY_LENGTH = 1024

# One span per call. crawl polls internally, so it must not emit one span per page.
_CRAWL_METHODS = {"crawl", "start_crawl", "get_crawl_status", "cancel_crawl"}


def _redact(value: str, instance: Any) -> str:
    api_key = getattr(instance, "api_key", None)
    if isinstance(api_key, str) and api_key:
        value = value.replace(api_key, "[redacted]")
    return value[:_MAX_QUERY_LENGTH]


def _url_host(value: Any, instance: Any) -> str:
    """Record only the host. The path and body stay off the span."""
    if not isinstance(value, str) or not value:
        return ""
    host = urlsplit(value).hostname or ""
    return _redact(host, instance)


def _document_count(result: Any) -> int:
    for attribute in ("data", "results", "web"):
        value = getattr(result, attribute, None)
        if value is not None:
            try:
                return len(value)
            except TypeError:
                return 0
    return 0


def _page_count(result: Any) -> int:
    count = getattr(result, "completed", None)
    if isinstance(count, int):
        return count
    data = getattr(result, "data", None)
    if data is not None:
        try:
            return len(data)
        except TypeError:
            return 0
    return 0


def _job_id(result: Any, kwargs: Mapping[str, Any]) -> str:
    for attribute in ("id", "job_id"):
        value = getattr(result, attribute, None)
        if isinstance(value, str) and value:
            return value
    for key in ("job_id", "crawl_id"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class _BaseWrapper:
    def __init__(self, tracer: Tracer, span_name: str, method_name: str) -> None:
        self._tracer = tracer
        self._span_name = span_name
        self._method_name = method_name

    def _attributes(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        attributes = {_FI_SPAN_KIND: _TOOL}
        if self._method_name == "search":
            query = kwargs.get("query", args[0] if args else "")
            attributes[_RETRIEVAL_QUERY] = _redact(str(query or ""), instance)
        elif self._method_name in ("scrape", "map", "crawl", "start_crawl"):
            url = kwargs.get("url", args[0] if args else "")
            host = _url_host(url, instance)
            if host:
                attributes["server.address"] = host
        if self._method_name in _CRAWL_METHODS:
            limit = kwargs.get("limit")
            if isinstance(limit, int):
                attributes[_LIMIT] = limit
            job_id = _job_id(None, kwargs)
            if job_id:
                attributes[_JOB_ID] = _redact(job_id, instance)
        return attributes

    def _finish_ok(self, span: Span, result: Any, kwargs: Mapping[str, Any]) -> None:
        if self._method_name == "search":
            span.set_attribute(_RETRIEVAL_DOCUMENT_COUNT, _document_count(result))
        if self._method_name in _CRAWL_METHODS:
            job_id = _job_id(result, kwargs)
            if job_id:
                span.set_attribute(_JOB_ID, _redact(job_id, span))
            if self._method_name == "crawl":
                span.set_attribute(_PAGE_COUNT, _page_count(result))
        credits = getattr(result, "credits_used", None)
        if isinstance(credits, int):
            span.set_attribute(_CREDITS_USED, credits)
        span.set_status(Status(StatusCode.OK))
        span.end()

    @staticmethod
    def _finish_error(span: Span, error: BaseException) -> None:
        span.record_exception(error)
        span.set_status(
            Status(StatusCode.ERROR, "{0}: {1}".format(type(error).__name__, error))
        )
        span.end()


class OperationWrapper(_BaseWrapper):
    """Trace a synchronous Firecrawl v2 operation."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        span = self._tracer.start_span(
            self._span_name, attributes=self._attributes(instance, args, kwargs)
        )
        try:
            result = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        self._finish_ok(span, result, kwargs)
        return result


class AsyncOperationWrapper(_BaseWrapper):
    """Trace an asynchronous Firecrawl v2 operation."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        span = self._tracer.start_span(
            self._span_name, attributes=self._attributes(instance, args, kwargs)
        )
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        self._finish_ok(span, result, kwargs)
        return result
