"""wrapt wrappers for Parallel.search and Parallel.extract (sync and async)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import Sequence as _SequenceABC
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

FI_SPAN_KIND = "fi.span.kind"
RETRIEVER = "RETRIEVER"
INPUT_VALUE = "input.value"
RETRIEVAL_QUERY = "gen_ai.retrieval.query"
MODE = "parallel.mode"
QUERY_COUNT = "parallel.query_count"
URL_COUNT = "parallel.url_count"
URLS = "parallel.urls"
OBJECTIVE = "parallel.objective"
RESULT_COUNT = "parallel.result_count"
FAILED_URL_COUNT = "parallel.failed_url_count"
SEARCH_ID = "parallel.search_id"
EXTRACT_ID = "parallel.extract_id"
SESSION_ID = "parallel.session_id"
USAGE_NAMES = "parallel.usage.names"
USAGE_COUNTS = "parallel.usage.counts"
WARNING_COUNT = "parallel.warning_count"
WARNING_EVENT = "parallel.warning"
WARNING_TYPE = "parallel.warning.type"
WARNING_MESSAGE = "parallel.warning.message"
CANCELLED = "parallel.cancelled"

SEARCH = "search"
EXTRACT = "extract"
REDACTED = "[redacted]"
MAX_VALUE_BYTES = 1024
MAX_NAME_BYTES = 256
MAX_STACKTRACE_BYTES = 16 * 1024
MAX_CAPTURED_URLS = 20
MAX_WARNING_EVENTS = 20
_API_KEY_HEADER = "x-api-key"


@dataclass(frozen=True)
class Options:
    """What a span may carry beyond counts and ids. Everything is off by default."""

    capture_urls: bool = False
    capture_objective: bool = False
    hide_inputs: bool = False
    hide_outputs: bool = False


class _State:
    """Shared by every wrapper of one instrument() call; uninstrument() disables it.

    parallel-web copies bound methods into ``with_raw_response`` and
    ``with_streaming_response``; a copy taken while instrumented must stop
    tracing once the instrumentor is removed.
    """

    def __init__(self) -> None:
        self.enabled = True


def _cap(value: str, limit: int = MAX_VALUE_BYTES) -> str:
    """Return the longest whole-character prefix of at most ``limit`` UTF-8 bytes."""
    # A cut inside a multi-byte character leaves an invalid tail; "ignore"
    # drops it. "replace" keeps a lone surrogate from raising.
    return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _redact(value: str, keys: Sequence[str]) -> str:
    for key in keys:
        value = value.replace(key, REDACTED)
    return value


def _clean(value: str, keys: Sequence[str], limit: int = MAX_VALUE_BYTES) -> str:
    """Redact first, then cap, so a key cut by the cap cannot leave a prefix."""
    return _cap(_redact(value, keys), limit)


def _api_keys(instance: Any, kwargs: Mapping[str, Any]) -> List[str]:
    """Every Parallel key this call could send, longest first.

    parallel-web keeps the key in ``client.api_key`` (also read from
    PARALLEL_API_KEY) and sends it as ``x-api-key`` from ``auth_headers``.
    A caller can also set that header through ``default_headers`` (stored in
    ``_custom_headers``, including PARALLEL_CUSTOM_HEADERS) or per call
    through ``extra_headers``.
    """
    keys: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)

    add(getattr(instance, "api_key", None))
    for source in (
        lambda: getattr(instance, "auth_headers", None),
        lambda: getattr(instance, "_custom_headers", None),
        lambda: kwargs.get("extra_headers"),
    ):
        try:
            headers = source()
            if isinstance(headers, Mapping):
                for name, value in headers.items():
                    if isinstance(name, str) and name.lower() == _API_KEY_HEADER:
                        add(value)
        except Exception:  # a header source must never break the call
            logger.debug("Could not read Parallel headers for redaction", exc_info=True)
    return sorted(keys, key=len, reverse=True)


def _sequence(value: Any) -> Optional[List[Any]]:
    """Return a list copy of a sequence argument without iterating anything else.

    A generator or other one-shot iterable is never consumed: the SDK must
    still receive it untouched, so its length is unknown.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, _SequenceABC) and not isinstance(value, (bytes, bytearray)):
        return list(value)
    return None


def _request_attributes(
    operation: str,
    instance: Any,
    kwargs: Mapping[str, Any],
    keys: Sequence[str],
    options: Options,
) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {FI_SPAN_KIND: RETRIEVER}
    if operation == SEARCH:
        mode = kwargs.get("mode")
        if isinstance(mode, str):
            attributes[MODE] = _clean(mode, keys, MAX_NAME_BYTES)
    else:
        urls = _sequence(kwargs.get("urls"))
        if urls is not None:
            attributes[URL_COUNT] = len(urls)
            if options.capture_urls and not options.hide_inputs:
                attributes[URLS] = [
                    _clean(url, keys) for url in urls[:MAX_CAPTURED_URLS] if isinstance(url, str)
                ]

    queries = _sequence(kwargs.get("search_queries"))
    if queries is not None:
        attributes[QUERY_COUNT] = len(queries)
        if queries and not options.hide_inputs:
            text = _clean("\n".join(str(query) for query in queries), keys)
            attributes[RETRIEVAL_QUERY] = text
            # The backend input panel reads input.value; keep both keys equal.
            attributes[INPUT_VALUE] = text

    objective = kwargs.get("objective")
    if options.capture_objective and not options.hide_inputs and isinstance(objective, str):
        attributes[OBJECTIVE] = _clean(objective, keys)

    session_id = kwargs.get("session_id")
    if isinstance(session_id, str) and session_id:
        attributes[SESSION_ID] = _clean(session_id, keys, MAX_NAME_BYTES)
    return attributes


def _count(value: Any) -> Optional[int]:
    """Length of a returned list; None (never 0) when the shape is unknown."""
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def _response_attributes(
    operation: str,
    result: Any,
    keys: Sequence[str],
    options: Options,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Counts, ids, usage and warnings from a SearchResult or ExtractResponse.

    Excerpts, titles, URLs and full page content are never read. A raw or
    streaming response (``with_raw_response`` / ``with_streaming_response``)
    has none of these fields and is not parsed, so it yields nothing.
    """
    attributes: Dict[str, Any] = {}
    results = _count(getattr(result, "results", None))
    if results is not None:
        attributes[RESULT_COUNT] = results
    if operation == EXTRACT:
        failed = _count(getattr(result, "errors", None))
        if failed is not None:
            attributes[FAILED_URL_COUNT] = failed

    id_field, id_key = ("search_id", SEARCH_ID) if operation == SEARCH else ("extract_id", EXTRACT_ID)
    for field, key in ((id_field, id_key), ("session_id", SESSION_ID)):
        value = getattr(result, field, None)
        if isinstance(value, str) and value:
            attributes[key] = _clean(value, keys, MAX_NAME_BYTES)

    usage = getattr(result, "usage", None)
    if isinstance(usage, (list, tuple)):
        names: List[str] = []
        counts: List[int] = []
        for item in usage:
            name = getattr(item, "name", None)
            count = getattr(item, "count", None)
            if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool):
                names.append(_cap(name, MAX_NAME_BYTES))
                counts.append(count)
        if names:
            attributes[USAGE_NAMES] = names
            attributes[USAGE_COUNTS] = counts

    events: List[Dict[str, Any]] = []
    warnings = getattr(result, "warnings", None)
    if isinstance(warnings, (list, tuple)) and warnings:
        attributes[WARNING_COUNT] = len(warnings)
        for warning in warnings[:MAX_WARNING_EVENTS]:
            event: Dict[str, Any] = {}
            kind = getattr(warning, "type", None)
            if isinstance(kind, str):
                event[WARNING_TYPE] = _cap(kind, MAX_NAME_BYTES)
            message = getattr(warning, "message", None)
            if isinstance(message, str) and not options.hide_outputs:
                event[WARNING_MESSAGE] = _clean(message, keys)
            events.append(event)
    return attributes, events


def _describe(error: BaseException) -> str:
    try:
        return str(error)
    except Exception:
        return "<unprintable {0}>".format(type(error).__name__)


def _exception_attributes(error: BaseException, keys: Sequence[str]) -> Dict[str, Any]:
    """The OTel exception event, with the Parallel key removed from every text.

    The message is then cut to 1 KB and the stacktrace to 16 KB of UTF-8: a
    server error can echo a large request into both.
    """
    error_type = type(error)
    module = error_type.__module__
    qualified = (
        "{0}.{1}".format(module, error_type.__qualname__)
        if module and module != "builtins"
        else error_type.__qualname__
    )
    stacktrace = "".join(traceback.format_exception(error_type, error, error.__traceback__))
    return {
        "exception.type": qualified,
        "exception.message": _clean(_describe(error), keys),
        "exception.stacktrace": _clean(stacktrace, keys, MAX_STACKTRACE_BYTES),
    }


@contextlib.contextmanager
def _current(span: Span) -> Iterator[None]:
    """Make ``span`` current for the vendor call so HTTP client spans nest under it.

    The span is ended by the wrapper, not on exit, and errors are recorded
    once by ``_Call.error``. A failure to attach the context leaves the call
    running without it rather than raising into the caller.
    """
    manager = None
    try:
        manager = trace_api.use_span(
            span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        )
        manager.__enter__()
    except Exception:
        logger.debug("Could not make the Parallel span current", exc_info=True)
        manager = None
    try:
        yield
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                logger.debug("Could not restore the trace context", exc_info=True)


class _Call:
    """One traced Parallel call. Every method is isolated and ends the span once."""

    def __init__(self, span: Span, operation: str, keys: List[str], options: Options) -> None:
        self.span = span
        self.operation = operation
        self.keys = keys
        self.options = options

    def ok(self, result: Any) -> None:
        try:
            attributes, events = _response_attributes(
                self.operation, result, self.keys, self.options
            )
            for key, value in attributes.items():
                self.span.set_attribute(key, value)
            for event in events:
                self.span.add_event(WARNING_EVENT, event)
        except Exception:
            logger.debug("Could not read the Parallel response", exc_info=True)
        try:
            self.span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("Could not set the span status", exc_info=True)
        self._end()

    def error(self, error: BaseException) -> None:
        # No result count: nothing was returned, so the count is unknown.
        try:
            description = "{0}: {1}".format(
                type(error).__name__, _clean(_describe(error), self.keys)
            )
            self.span.set_status(Status(StatusCode.ERROR, description))
        except Exception:
            logger.debug("Could not set the error status", exc_info=True)
        try:
            self.span.add_event("exception", _exception_attributes(error, self.keys))
        except Exception:
            logger.debug("Could not record the exception", exc_info=True)
        self._end()

    def cancelled(self) -> None:
        # Cancellation is not an exception: no event and no result count.
        try:
            self.span.set_attribute(CANCELLED, True)
            self.span.set_status(Status(StatusCode.ERROR, "cancelled"))
        except Exception:
            logger.debug("Could not mark the span cancelled", exc_info=True)
        self._end()

    def _end(self) -> None:
        try:
            self.span.end()
        except Exception:
            logger.debug("Could not end the Parallel span", exc_info=True)


class _BaseWrapper:
    def __init__(
        self,
        tracer: Tracer,
        operation: str,
        options: Options,
        state: Optional[_State] = None,
    ) -> None:
        self._tracer = tracer
        self._operation = operation
        self._span_name = "parallel.{0}".format(operation)
        self._options = options
        self._state = state or _State()

    def _start(self, instance: Any, kwargs: Mapping[str, Any]) -> Optional[_Call]:
        """Start the span, or return None to run the call untraced."""
        if not self._state.enabled:
            return None
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return None
        keys: List[str] = []
        try:
            keys = _api_keys(instance, kwargs)
            attributes = _request_attributes(
                self._operation, instance, kwargs, keys, self._options
            )
        except Exception:  # an attribute must never break the user's call
            logger.debug("Could not read the Parallel request", exc_info=True)
            attributes = {FI_SPAN_KIND: RETRIEVER}
        try:
            span = self._tracer.start_span(self._span_name, attributes=attributes)
        except Exception:
            logger.debug("Could not start the Parallel span", exc_info=True)
            return None
        return _Call(span, self._operation, keys, self._options)


class OperationWrapper(_BaseWrapper):
    """Trace a synchronous ``Parallel.search`` / ``Parallel.extract`` call."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        call = self._start(instance, kwargs)
        if call is None:
            return wrapped(*args, **kwargs)
        try:
            with _current(call.span):
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            call.error(error)
            raise
        call.ok(result)
        return result


class AsyncOperationWrapper(_BaseWrapper):
    """Trace an ``AsyncParallel.search`` / ``AsyncParallel.extract`` call."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        call = self._start(instance, kwargs)
        if call is None:
            return await wrapped(*args, **kwargs)
        try:
            with _current(call.span):
                result = await wrapped(*args, **kwargs)
        except asyncio.CancelledError:
            call.cancelled()
            raise
        except BaseException as error:
            call.error(error)
            raise
        call.ok(result)
        return result
