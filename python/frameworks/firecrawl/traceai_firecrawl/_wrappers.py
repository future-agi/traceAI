"""wrapt wrappers for the supported Firecrawl v2 client methods."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from contextvars import ContextVar
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from fi_instrumentation.instrumentation import TraceConfig
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string

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
_FORMATS = "firecrawl.formats"
_ERROR_STATUS_CODE = "firecrawl.error.status_code"
_ERROR_CODE = "firecrawl.error.code"
_MAX_QUERY_LENGTH = 1024

# One span per call. crawl polls internally, so it must not emit one span per page.
_CRAWL_METHODS = {"crawl", "start_crawl", "get_crawl_status", "cancel_crawl"}
_JOB_ID_FIRST_ARG_METHODS = {"get_crawl_status", "cancel_crawl"}
# Methods that return a CrawlJob. The SDK returns failed and cancelled jobs
# without raising (methods/crawl.py wait_for_crawl_completion), so the span
# status comes from the job.
_JOB_STATUS_METHODS = {"crawl", "get_crawl_status"}
# Of those, the methods whose span status follows the job (crawl waits for it).
_JOB_OUTCOME_METHODS = {"crawl"}
_ERROR_JOB_STATUSES = {"failed", "cancelled"}
# Methods whose arguments carry the requested output formats.
_FORMAT_METHODS = {"scrape", "search", "crawl", "start_crawl"}
# SDK 4.46.2 FormatString/model Literals and validation aliases. The finite
# vocabulary bounds both label length and the deduplicated attribute list.
_FORMAT_NAMES = {name: name for name in (
    "markdown", "html", "rawHtml", "links", "images", "screenshot", "summary",
    "changeTracking", "json", "attributes", "branding", "product", "menu",
    "query", "audio", "video", "question", "highlights", "screenshot@fullPage",
)}
_FORMAT_NAMES.update(raw_html="rawHtml", change_tracking="changeTracking",
                     screenshot_full_page="screenshot@fullPage")
_CANCELLATION_ERRORS = (asyncio.CancelledError, concurrent.futures.CancelledError)
# Fixed metadata vocabulary: arbitrary exception names/codes are caller content.
_SAFE_EXCEPTION_TYPES = frozenset({
    "Exception", "RuntimeError", "ValueError", "TypeError", "TimeoutError",
    "CancelledError", "ValidationError", "FirecrawlError", "BadRequestError",
    "UnauthorizedError", "PaymentRequiredError", "WebsiteNotSupportedError",
    "ProviderTermsRequiredError", "RequestTimeoutError", "CrawlJobTimeoutError",
    "RateLimitError", "InternalServerError",
})
_SAFE_ERROR_CODES = frozenset({
    "RATE_LIMIT_EXCEEDED", "THIRD_PARTY_DATA_TERMS_REQUIRED", "duplicate_request",
    "request_in_flight", "request_unresolved", "unknown_provider",
    "insufficient_credits", "billing_unavailable",
})

# True while a traced Firecrawl call runs in this context. firecrawl-py 4.46.2
# AsyncFirecrawlClient.crawl awaits self.start_crawl, which is wrapped as well;
# the nested call runs untraced so one user call yields one span. A context
# variable follows asyncio tasks, so concurrent calls do not suppress each other.
_ACTIVE: ContextVar[bool] = ContextVar("traceai_firecrawl_active", default=False)


def _api_keys(instance: Any) -> list[str]:
    """API keys the client holds. firecrawl-py 4.46.2 keeps the key on
    ``http_client`` (sync and async), ``async_http_client`` (async) and
    ``config`` (sync), not on the client; it also resolves FIRECRAWL_API_KEY
    into those objects."""
    keys: list[str] = []
    for holder in (
        instance,
        getattr(instance, "http_client", None),
        getattr(instance, "async_http_client", None),
        getattr(instance, "config", None),
    ):
        if holder is None:
            continue
        api_key = getattr(holder, "api_key", None)
        if isinstance(api_key, str) and api_key and api_key not in keys:
            keys.append(api_key)
    return keys


def _redact(value: str, instance: Any, config: Optional[TraceConfig] = None) -> Optional[str]:
    try:
        keys = _api_keys(instance)
    except Exception:
        logger.debug("Could not discover Firecrawl API keys; omitting free text")
        return None
    for api_key in keys:
        value = value.replace(api_key, "[redacted]")
    if config is not None and config.pii_redaction:
        value = redact_pii_in_string(value)
    return value[:_MAX_QUERY_LENGTH]


def _url_host(value: Any, instance: Any) -> str:
    """Record only the host. The path and body stay off the span."""
    if not isinstance(value, str) or not value:
        return ""
    host = urlsplit(value).hostname or ""
    return _redact(host, instance) or ""


def _document_count(result: Any) -> Optional[int]:
    from firecrawl.v2.types import SearchData

    if isinstance(result, SearchData):
        count = 0
        for group in ("web", "news", "images", "tools"):
            records = getattr(result, group, None)
            if records is not None:
                if not isinstance(records, (list, tuple)):
                    return None
                count += len(records)
        return count
    # Preserve the prior generic list-result contract; strings, mappings and
    # unrecognized objects do not establish a measured result count.
    for attribute in ("data", "results", "web"):
        value = getattr(result, attribute, None)
        if value is not None:
            return len(value) if isinstance(value, (list, tuple)) else None
    return None


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


def _formats(kwargs: Mapping[str, Any], method_name: str) -> list[str]:
    """Names of the requested formats. A format object's prompt or schema is
    content, so only its ``type`` is kept."""
    from firecrawl.v2.types import ScrapeFormats

    scrape_options = kwargs.get("scrape_options")
    # The SDK ignores convenience formats when scrape_options is supplied.
    # Search has no supported top-level formats argument.
    if scrape_options is not None or method_name == "search":
        if isinstance(scrape_options, Mapping):
            formats = scrape_options.get("formats")
        else:
            formats = getattr(scrape_options, "formats", None)
    else:
        formats = kwargs.get("formats")
    if isinstance(formats, ScrapeFormats):
        container = formats
        formats = list(container.formats or ())
        # Match prepare_scrape_options in 4.46.2: images/json flags are not
        # serialized. Its markdown default is serialized, unless disabled.
        for flag in ("markdown", "html", "raw_html", "summary", "links",
                     "screenshot", "change_tracking"):
            if getattr(container, flag) is True:
                formats.append(flag)
    if not isinstance(formats, (list, tuple)):
        return []
    names = []
    for item in formats:
        if isinstance(item, str):
            name = item
        elif isinstance(item, Mapping):
            name = item.get("type")
        else:
            name = getattr(item, "type", None)
        if isinstance(name, str):
            canonical = _FORMAT_NAMES.get(name)
            if canonical is not None and canonical not in names:
                names.append(canonical)
    return names


def _vendor_error_fields(error: BaseException) -> tuple[Optional[int], Optional[str]]:
    """HTTP status and machine-readable code of a firecrawl-py FirecrawlError."""
    try:
        from firecrawl.v2.utils.error_handler import FirecrawlError
    except Exception:
        return None, None
    if not isinstance(error, FirecrawlError):
        return None, None
    status_code = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    return (
        status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else None,
        code if isinstance(code, str) and code in _SAFE_ERROR_CODES else None,
    )


class _BaseWrapper:
    """Span bookkeeping shared by the sync and async wrappers.

    Everything here is isolated from the caller: an error while reading
    arguments, reading the result or recording an exception is logged at debug
    level and never replaces the vendor's own result or exception. Every span
    that starts is ended exactly once.
    """

    def __init__(self, tracer: Tracer, span_name: str, method_name: str, config: TraceConfig) -> None:
        self._tracer = tracer
        self._span_name = span_name
        self._method_name = method_name
        self._config = config

    def _attributes(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        attributes = {_FI_SPAN_KIND: _TOOL}
        if self._method_name == "search":
            if not (self._config.hide_inputs or self._config.hide_input_text):
                query = kwargs.get("query", args[0] if args else "")
                query_text = _redact(str(query or ""), instance, self._config)
                if query_text is not None:
                    attributes[_RETRIEVAL_QUERY] = query_text
        elif self._method_name in ("scrape", "map", "crawl", "start_crawl"):
            url = kwargs.get("url", args[0] if args else "")
            host = _url_host(url, instance)
            if host:
                attributes["server.address"] = host
        if self._method_name in _FORMAT_METHODS:
            formats = _formats(kwargs, self._method_name)
            if formats:
                attributes[_FORMATS] = formats
        if self._method_name in _CRAWL_METHODS:
            limit = kwargs.get("limit")
            if isinstance(limit, int):
                attributes[_LIMIT] = limit
            job_id = _call_job_id(self._method_name, args, kwargs)
            if job_id:
                safe_id = _redact(job_id, instance)
                if safe_id is not None:
                    attributes[_JOB_ID] = safe_id
        return attributes

    def _start(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> Optional[Span]:
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

    def _record_result(
        self, span: Span, instance: Any, result: Any, kwargs: Mapping[str, Any]
    ) -> None:
        if self._method_name == "search":
            count = _document_count(result)
            if count is not None:
                span.set_attribute(_RETRIEVAL_DOCUMENT_COUNT, count)
        if self._method_name in _CRAWL_METHODS:
            job_id = _job_id(result, kwargs)
            if job_id:
                safe_id = _redact(job_id, instance)
                if safe_id is not None:
                    span.set_attribute(_JOB_ID, safe_id)
            if self._method_name == "crawl":
                span.set_attribute(_PAGE_COUNT, _page_count(result))
        if self._method_name == "cancel_crawl" and isinstance(result, bool):
            span.set_attribute(_CANCELLED, result)
        credits = getattr(result, "credits_used", None)
        if isinstance(credits, int):
            span.set_attribute(_CREDITS_USED, credits)
        job_status = (
            getattr(result, "status", None) if self._method_name in _JOB_STATUS_METHODS else None
        )
        if isinstance(job_status, str) and job_status:
            span.set_attribute(_STATUS, job_status)
        # Only the blocking crawl() call's outcome is the job's outcome. A
        # get_crawl_status poll that got an answer succeeded; it records the state.
        job_failed = self._method_name in _JOB_OUTCOME_METHODS and job_status in _ERROR_JOB_STATUSES
        if job_failed and job_status == "cancelled":
            span.set_attribute(_CANCELLED, True)
        if job_failed:
            span.set_status(Status(StatusCode.ERROR, job_status))
        else:
            span.set_status(Status(StatusCode.OK))

    def _finish_ok(
        self, span: Span, instance: Any, result: Any, kwargs: Mapping[str, Any]
    ) -> None:
        try:
            self._record_result(span, instance, result, kwargs)
        except Exception:
            logger.debug("Could not read %s result", self._span_name, exc_info=True)
        finally:
            _end(span)

    def _finish_error(self, span: Span, error: BaseException, instance: Any = None) -> None:
        cancelled = isinstance(error, _CANCELLATION_ERRORS)
        try:
            if cancelled:
                span.set_attribute(_CANCELLED, True)
            status_code, code = _vendor_error_fields(error)
            if status_code is not None:
                span.set_attribute(_ERROR_STATUS_CODE, status_code)
            if code is not None:
                safe_code = _redact(code, instance)
                if safe_code is not None:
                    span.set_attribute(_ERROR_CODE, safe_code)
        except Exception:
            logger.debug("Could not read %s error", self._span_name, exc_info=True)
        try:
            span.add_event("exception", {
                "exception.type": _exception_type(error),
                "exception.message": "Firecrawl call failed",
            })
        except Exception:
            logger.debug("Could not record %s exception", self._span_name, exc_info=True)
        try:
            description = "cancelled" if cancelled else _exception_type(error)
            span.set_status(Status(StatusCode.ERROR, description))
        except Exception:
            logger.debug("Could not set %s status", self._span_name, exc_info=True)
        finally:
            _end(span)


def _exception_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if name in _SAFE_EXCEPTION_TYPES else "Exception"


def _current(span: Span) -> Any:
    """Make the span current for the vendor call so HTTP client spans nest under
    it. The wrapper records the exception and ends the span itself."""
    return trace_api.use_span(
        span, end_on_exit=False, record_exception=False, set_status_on_exception=False
    )


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
            with _current(span):
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error, instance)
            raise
        finally:
            _ACTIVE.reset(token)
        self._finish_ok(span, instance, result, kwargs)
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
            with _current(span):
                result = await wrapped(*args, **kwargs)
        except BaseException as error:
            self._finish_error(span, error, instance)
            raise
        finally:
            _ACTIVE.reset(token)
        self._finish_ok(span, instance, result, kwargs)
        return result
