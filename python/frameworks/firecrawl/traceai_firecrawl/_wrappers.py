"""wrapt wrappers for the supported Firecrawl v2 client methods."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from opentelemetry.trace import Span, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

_FI_SPAN_KIND = "fi.span.kind"
_TOOL = "TOOL"
_RETRIEVAL_QUERY = "fi.retrieval.query"
_RETRIEVAL_DOCUMENT_COUNT = "fi.retrieval.document_count"
_JOB_ID = "firecrawl.job_id"
_LIMIT = "firecrawl.limit"
_PAGE_COUNT = "firecrawl.page_count"
_CREDITS_USED = "firecrawl.credits_used"
_STATUS = "firecrawl.status"
_CANCELLED = "firecrawl.cancelled"
_MAX_QUERY_LENGTH = 1024

# One span per call. crawl polls internally, so it must not emit one span per page.
_CRAWL_METHODS = {"crawl", "start_crawl", "get_crawl_status", "cancel_crawl"}
_JOB_ID_FIRST_ARG_METHODS = {"get_crawl_status", "cancel_crawl"}
# Methods that return a CrawlJob. The SDK returns failed and cancelled jobs
# without raising (methods/crawl.py wait_for_crawl_completion), so the span
# status comes from the job.
_JOB_STATUS_METHODS = {"crawl", "get_crawl_status"}
_ERROR_JOB_STATUSES = {"failed", "cancelled"}

# True while a traced Firecrawl call runs in this context. firecrawl-py 4.46.2
# AsyncFirecrawlClient.crawl awaits self.start_crawl, which is wrapped as well;
# the nested call runs untraced so one user call yields one span. A context
# variable follows asyncio tasks, so concurrent calls do not suppress each other.
_ACTIVE: ContextVar[bool] = ContextVar("traceai_firecrawl_active", default=False)


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


def _call_job_id(method_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    """Job id the caller passed. get_crawl_status(job_id) and cancel_crawl(crawl_id
    sync / job_id async) take it as the first positional argument."""
    if method_name in _JOB_ID_FIRST_ARG_METHODS and args:
        value = args[0]
        if isinstance(value, str) and value:
            return value
    return _job_id(None, kwargs)


class _BaseWrapper:
    """Span bookkeeping shared by the sync and async wrappers.

    Everything here is isolated from the caller: an error while reading
    arguments, reading the result or recording an exception is logged at debug
    level and never replaces the vendor's own result or exception. Every span
    that starts is ended exactly once.
    """

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
            job_id = _call_job_id(self._method_name, args, kwargs)
            if job_id:
                attributes[_JOB_ID] = _redact(job_id, instance)
        return attributes

    def _start(self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Optional[Span]:
        try:
            attributes = self._attributes(instance, args, kwargs)
        except Exception:
            logger.debug("Could not read %s arguments", self._span_name, exc_info=True)
            attributes = {_FI_SPAN_KIND: _TOOL}
        try:
            return self._tracer.start_span(self._span_name, attributes=attributes)
        except Exception:
            logger.debug("Could not start %s span", self._span_name, exc_info=True)
            return None

    def _record_result(self, span: Span, result: Any, kwargs: Mapping[str, Any]) -> None:
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
        job_status = (
            getattr(result, "status", None) if self._method_name in _JOB_STATUS_METHODS else None
        )
        if isinstance(job_status, str) and job_status:
            span.set_attribute(_STATUS, job_status)
        if job_status == "cancelled":
            span.set_attribute(_CANCELLED, True)
        if job_status in _ERROR_JOB_STATUSES:
            span.set_status(Status(StatusCode.ERROR, job_status))
        else:
            span.set_status(Status(StatusCode.OK))

    def _finish_ok(self, span: Span, result: Any, kwargs: Mapping[str, Any]) -> None:
        try:
            self._record_result(span, result, kwargs)
        except Exception:
            logger.debug("Could not read %s result", self._span_name, exc_info=True)
        finally:
            _end(span)

    def _finish_error(self, span: Span, error: BaseException) -> None:
        try:
            span.record_exception(error)
        except Exception:
            logger.debug("Could not record %s exception", self._span_name, exc_info=True)
        try:
            span.set_status(Status(StatusCode.ERROR, _describe(error)))
        except Exception:
            logger.debug("Could not set %s status", self._span_name, exc_info=True)
        finally:
            _end(span)


def _describe(error: BaseException) -> str:
    try:
        return "{0}: {1}".format(type(error).__name__, error)
    except Exception:
        return type(error).__name__


def _end(span: Span) -> None:
    try:
        span.end()
    except Exception:
        logger.debug("Could not end span", exc_info=True)


class OperationWrapper(_BaseWrapper):
    """Trace a synchronous Firecrawl v2 operation."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
    ) -> Any:
        if _ACTIVE.get():
            return wrapped(*args, **kwargs)
        span = self._start(instance, args, kwargs)
        if span is None:
            return wrapped(*args, **kwargs)
        token = _ACTIVE.set(True)
        try:
            result = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        finally:
            _ACTIVE.reset(token)
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
        if _ACTIVE.get():
            return await wrapped(*args, **kwargs)
        span = self._start(instance, args, kwargs)
        if span is None:
            return await wrapped(*args, **kwargs)
        token = _ACTIVE.set(True)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error)
            raise
        finally:
            _ACTIVE.reset(token)
        self._finish_ok(span, result, kwargs)
        return result
