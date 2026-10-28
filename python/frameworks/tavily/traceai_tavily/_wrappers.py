"""wrapt wrappers for TavilyClient / AsyncTavilyClient search and extract.

Everything the wrapper does for itself (reading the client, reading the
arguments, starting and ending the span, reading the result, recording an
error) is isolated from the caller: a failure there is logged at debug level
and never replaces the vendor's own result or exception.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

_SPAN_KIND = "gen_ai.span.kind"
_TOOL = "TOOL"
_RETRIEVER = "RETRIEVER"
_RETRIEVAL_QUERY = "gen_ai.retrieval.query"
_TOOL_NAME = "gen_ai.tool.name"
_INPUT_VALUE = "input.value"
_URL_COUNT = "tavily.url_count"
_RESULT_COUNT = "tavily.result_count"
_FAILED_RESULT_COUNT = "tavily.failed_result_count"
_CANCELLED = "tavily.cancelled"
_CANCELLATION_ERRORS = (asyncio.CancelledError, concurrent.futures.CancelledError)
_REDACTED = "[redacted]"
# What fi_instrumentation's hide_inputs writes in place of input.value.
_HIDDEN = "__REDACTED__"
_MAX_TEXT_BYTES = 1024


def _bearer_token(value: Any) -> Optional[str]:
    """The key in an ``Authorization`` header value (``Bearer <key>``)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value[:7].lower() == "bearer ":
        value = value[7:].strip()
    return value or None


def _api_keys(instance: Any) -> Optional[List[str]]:
    """Every Tavily key the client holds, longest first; None if unknown.

    tavily-python 0.8.4 keeps it in ``api_key``, ``headers["Authorization"]``
    and the requests session's headers (TavilyClient), or only in the httpx
    client's headers (AsyncTavilyClient, ``_client``). A caller's own session
    or httpx client can carry a key that ``api_key`` does not. If any of those
    places cannot be read, the result is None and no free text is recorded:
    a key there could not be removed from it.
    """
    try:
        candidates: List[Any] = [getattr(instance, "api_key", None)]
        for holder in (
            instance,
            getattr(instance, "session", None),
            getattr(instance, "_client", None),
        ):
            headers = getattr(holder, "headers", None) if holder is not None else None
            if headers is not None:
                candidates.append(_bearer_token(headers.get("Authorization")))
    except Exception:
        logger.debug("Could not read the Tavily client's key", exc_info=True)
        return None
    keys: List[str] = []
    for key in candidates:
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)
    return sorted(keys, key=len, reverse=True)


def _redact(text: str, keys: Sequence[str]) -> str:
    for key in keys:
        text = text.replace(key, _REDACTED)
    return text


def _scrub(text: str, keys: Sequence[str], hidden: Optional[str] = None) -> str:
    """``text`` with every key redacted, then every ``hidden`` query replaced."""
    text = _redact(text, keys)
    if hidden:
        text = text.replace(hidden, _HIDDEN)
    return text


def _safe_text(
    text: str, keys: Optional[Sequence[str]], hidden: Optional[str] = None, pii: bool = False
) -> Optional[str]:
    """``text`` scrubbed (see ``_scrub``), PII-redacted when asked, then capped.

    None when keys are unknown.
    """
    if keys is None:
        return None
    # Redact first, then cap, so a key or an email cut at the limit leaves no prefix.
    text = _scrub(text, keys, hidden)
    if pii:
        text = redact_pii_in_string(text)
    return _cap(text)


def _cap(text: str, limit: int = _MAX_TEXT_BYTES) -> str:
    """The longest whole-character prefix of at most ``limit`` UTF-8 bytes."""
    # A cut inside a multi-byte character leaves an invalid tail that "ignore"
    # drops; "replace" keeps a lone surrogate from raising.
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _function(wrapped: Any) -> Any:
    """The plain function behind what wrapt passes as ``wrapped``.

    ``client.extract(...)`` gives a bound method; ``TavilyClient.extract(client,
    ...)`` gives a wrapt partial proxy whose ``__wrapped__`` is the function.
    """
    function = getattr(wrapped, "__func__", None)
    if function is None:
        function = getattr(wrapped, "__wrapped__", wrapped)
    return function


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

    def __init__(
        self, tracer: Any, method: str, hide_inputs: bool = False, pii: bool = False
    ) -> None:
        self._tracer = tracer
        self._method = method
        # TraceConfig.hide_inputs: also keep the query out of error text.
        self._hide_inputs = hide_inputs
        # TraceConfig.pii_redaction: FITracer covers attributes; error text is done here.
        self._pii = pii
        # search is a retrieval; extract fetches the pages it is given.
        self._kind = _RETRIEVER if method == "search" else _TOOL
        self._span_name = "tavily.{0}".format(method)
        # Signature of each wrapped function, read once.
        self._signatures: Dict[Any, inspect.Signature] = {}

    def _query(
        self, wrapped: Any, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> Optional[str]:
        """The search query, or extract's optional rerank query, bound by name.

        None when there is none, or when the arguments cannot be bound to the
        method's signature or the value cannot be printed.
        """
        try:
            function = _function(wrapped)
            signature = self._signatures.get(function)
            if signature is None:
                signature = inspect.signature(function)
                self._signatures[function] = signature
            # The function's first parameter is ``self``.
            bound = signature.bind_partial(
                *((instance,) if instance is not None else ()), *args, **kwargs
            )
            value = bound.arguments.get("query")
            return None if value is None else str(value)
        except Exception:
            logger.debug("Could not read the %s query", self._span_name, exc_info=True)
            return None

    def _attributes(
        self,
        keys: Optional[Sequence[str]],
        query: Optional[str],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {_SPAN_KIND: self._kind, _TOOL_NAME: self._span_name}
        if query is not None:
            value = _safe_text(query, keys)
            if value is not None:
                attributes[_INPUT_VALUE] = value
                # FITracer masks input.value when inputs are hidden, but not this key.
                if self._method == "search" and not self._hide_inputs:
                    attributes[_RETRIEVAL_QUERY] = value
        if self._method == "extract":
            urls = _url_count(args, kwargs)
            if urls is not None:
                attributes[_URL_COUNT] = urls
        return attributes

    def _start(
        self,
        keys: Optional[Sequence[str]],
        query: Optional[str],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> Optional[Span]:
        try:
            attributes = self._attributes(keys, query, args, kwargs)
        except Exception:
            logger.debug("Could not read %s arguments", self._span_name, exc_info=True)
            attributes = {_SPAN_KIND: self._kind, _TOOL_NAME: self._span_name}
        try:
            return self._tracer.start_span(self._span_name, attributes=attributes)
        except Exception:
            logger.debug("Could not start the %s span", self._span_name, exc_info=True)
            return None

    def _finish_ok(self, span: Span, result: Any) -> None:
        try:
            count = _count(result, "results")
            if count is not None:
                span.set_attribute(_RESULT_COUNT, count)
            if self._method == "extract":
                failed = _count(result, "failed_results")
                if failed is not None:
                    span.set_attribute(_FAILED_RESULT_COUNT, failed)
        except Exception:
            logger.debug("Could not read the %s result", self._span_name, exc_info=True)
        try:
            span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("Could not set the %s status", self._span_name, exc_info=True)
        _end(span)

    def _finish_error(
        self,
        span: Span,
        error: BaseException,
        keys: Optional[Sequence[str]],
        query: Optional[str],
    ) -> None:
        try:
            if isinstance(error, _CANCELLATION_ERRORS):
                # Cancellation is not a failure of the call: no exception event.
                span.set_attribute(_CANCELLED, True)
                span.set_status(Status(StatusCode.ERROR, "cancelled"))
            else:
                self._record_error(span, error, keys, query)
        except Exception:
            logger.debug("Could not record the %s error", self._span_name, exc_info=True)
        _end(span)

    def _hidden_query(
        self, query: Optional[str], keys: Optional[Sequence[str]]
    ) -> Optional[str]:
        """The query to remove from error text: only with inputs hidden.

        It is the query as it reads after the key is redacted, because the key
        is removed from the text first. None when there is nothing to remove.
        """
        if not self._hide_inputs or not query or keys is None:
            return None
        return _redact(query, keys) or None

    def _record_error(
        self,
        span: Span,
        error: BaseException,
        keys: Optional[Sequence[str]],
        query: Optional[str],
    ) -> None:
        name = type(error).__name__
        try:
            hidden = self._hidden_query(query, keys)
            message = _safe_text(str(error), keys, hidden, self._pii)
        except Exception:
            hidden, message = None, None
        description = name if message is None else _cap("{0}: {1}".format(name, message))
        try:
            span.set_status(Status(StatusCode.ERROR, description))
        except Exception:
            logger.debug("Could not set the %s status", self._span_name, exc_info=True)
        # The SDK would copy str(error) and the raw traceback into the event.
        # A raw traceback carries local file paths, the username and code
        # frames, so it is never exported: "exception.stacktrace" is always a
        # frames-free summary (type and the same scrubbed message as the status),
        # and "exception.message" is the scrubbed message. The key (and, with
        # inputs hidden, the query) is removed from both.
        if message is None:
            event: Dict[str, Any] = {
                "exception.message": _REDACTED,
                "exception.stacktrace": name,
            }
        else:
            event = {"exception.message": message, "exception.stacktrace": description}
        try:
            span.record_exception(error, attributes=event)
        except Exception:
            logger.debug("Could not record the %s exception", self._span_name, exc_info=True)


def _end(span: Span) -> None:
    try:
        span.end()
    except Exception:
        logger.debug("Could not end the Tavily span", exc_info=True)


def _attach(span: Span) -> Any:
    """Make ``span`` current so HTTP client activity nests under it."""
    try:
        return context_api.attach(trace_api.set_span_in_context(span))
    except Exception:
        logger.debug("Could not make the Tavily span current", exc_info=True)
        return None


def _detach(token: Any) -> None:
    if token is None:
        return
    try:
        context_api.detach(token)
    except Exception:
        logger.debug("Could not restore the context", exc_info=True)


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
        query = self._query(wrapped, instance, args, kwargs)
        span = self._start(keys, query, args, kwargs)
        if span is None:
            return wrapped(*args, **kwargs)
        token = _attach(span)
        try:
            result = wrapped(*args, **kwargs)
        except BaseException as error:
            _detach(token)
            self._finish_error(span, error, keys, query)
            raise
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
        query = self._query(wrapped, instance, args, kwargs)
        span = self._start(keys, query, args, kwargs)
        if span is None:
            return await wrapped(*args, **kwargs)
        token = _attach(span)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as error:
            _detach(token)
            self._finish_error(span, error, keys, query)
            raise
        _detach(token)
        self._finish_ok(span, result)
        return result
