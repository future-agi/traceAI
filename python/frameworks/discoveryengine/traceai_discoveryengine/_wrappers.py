"""wrapt wrappers for Discovery Engine search, search_lite and answer_query (sync and async)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from fi_instrumentation import REDACTED_VALUE
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

FI_SPAN_KIND = "fi.span.kind"
RETRIEVER = "RETRIEVER"
INPUT_VALUE = "input.value"
RETRIEVAL_QUERY = "gen_ai.retrieval.query"
SERVING_CONFIG = "discoveryengine.serving_config"
RESULT_COUNT = "discoveryengine.result_count"
SESSION = "discoveryengine.session"
ANSWER_LENGTH = "discoveryengine.answer.length"
ANSWER_STATE = "discoveryengine.answer.state"
ERROR_STATUS = "discoveryengine.error.status"
ERROR_CODE = "discoveryengine.error.code"
CANCELLED = "discoveryengine.cancelled"

SEARCH = "search"
SEARCH_LITE = "search_lite"
ANSWER_QUERY = "answer_query"
REDACTED = "[redacted]"
# Recorded instead of server-written text when the credentials the client
# holds could not be read, so they cannot be removed from it.
UNREADABLE = "[not recorded: the client credentials could not be read]"
MAX_VALUE_BYTES = 1024
MAX_STACKTRACE_BYTES = 16 * 1024
_FAILED_STATE = "FAILED"

# gRPC metadata keys that carry a credential, compared in lower case.
_AUTH_METADATA = frozenset(
    {"authorization", "proxy-authorization", "x-goog-api-key", "x-goog-iam-authorization-token"}
)
# Credential attributes of google.auth credentials that may hold a secret.
_CREDENTIAL_FIELDS = ("token", "refresh_token", "client_secret")
# Google credential shapes, removed even when the package never held the
# value (a token minted by a refresh, a key quoted by the server).
_TOKEN_SHAPES = (
    re.compile(r"ya29\.[0-9A-Za-z_\-.~+/]+=*"),  # OAuth 2.0 access token
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # API key
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),  # OAuth 2.0 refresh token
    # JWT, such as a service account's self-signed token: three base64url
    # parts, the first a JSON object ("eyJ" is base64 of '{"').
    re.compile(r"(?<![0-9A-Za-z_\-])eyJ[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]+"),
)
# Only in server-written text: the value after "Bearer" in a quoted header.
_BEARER = re.compile(r"(?i)(\bbearer\s+)(?!\[redacted\])[^\s'\",;]+")

# Set while a traced call runs, so a wrapped method called from inside it
# (no 0.20.5 method does) does not open a second span.
_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "traceai_discoveryengine_active", default=False
)


@dataclass(frozen=True)
class Options:
    """What a span may carry beyond counts and resource names, and how text is cleaned.

    ``capture_query`` is off by default. ``hide_*`` and ``pii_redaction``
    come from the ``TraceConfig`` that ``FITracer`` also applies.
    """

    capture_query: bool = False
    hide_inputs: bool = False
    hide_outputs: bool = False
    pii_redaction: bool = False


class _State:
    """Shared by every wrapper of one instrument() call; uninstrument() disables it.

    A bound method read while instrumented (``search = client.search``) keeps
    the wrapper; it must stop tracing once the instrumentor is removed.
    """

    def __init__(self) -> None:
        self.enabled = True


def _cap(value: str, limit: int = MAX_VALUE_BYTES) -> str:
    """Return the longest whole-character prefix of at most ``limit`` UTF-8 bytes."""
    # A cut inside a multi-byte character leaves an invalid tail; "ignore"
    # drops it. "replace" keeps a lone surrogate from raising.
    return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def _scrub(value: str, secrets: Sequence[str], server: bool = False) -> str:
    """Replace every credential the client holds, then Google credential shapes.

    ``server`` also replaces the value after ``Bearer``; that pass runs only
    on server-written text, where a quoted header is the likely source.
    """
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    for shape in _TOKEN_SHAPES:
        value = shape.sub(REDACTED, value)
    if server:
        value = _BEARER.sub(lambda match: match.group(1) + REDACTED, value)
    return value


# The query text to remove from server-written text: () when it is recorded,
# or None when it must be removed but could not be read (then no such text
# is kept).
Hidden = Optional[Tuple[str, ...]]


def _hide(value: str, hidden: Hidden) -> str:
    """Replace every verbatim occurrence of a hidden input with ``__REDACTED__``."""
    if hidden is None:
        return REDACTED_VALUE
    if not hidden:
        return value
    pattern = "|".join(re.escape(text) for text in hidden)
    return re.sub(pattern, lambda _: REDACTED_VALUE, value)


def _clean(
    value: str,
    secrets: Sequence[str],
    limit: int = MAX_VALUE_BYTES,
    pii: bool = False,
    hidden: Hidden = (),
    server: bool = False,
) -> str:
    """Remove credentials, then the hidden query, then PII when enabled, then cap.

    Removing before the cap means a credential, the query or an email cut
    by the cap cannot leave a prefix. ``FITracer`` applies its PII pass to
    attributes again, after the cap; span events and the status are written
    only from here.
    """
    value = _hide(_scrub(value, secrets, server), hidden)
    if pii:
        value = redact_pii_in_string(value)
    return _cap(value, limit)


def _field(message: Any, name: str) -> Any:
    """A request or response field from a proto-plus message, raw protobuf or dict."""
    if message is None:
        return None
    if isinstance(message, Mapping):
        return message.get(name)
    return getattr(message, name, None)


def _has(message: Any, name: str) -> bool:
    """Whether a message field is set (proto-plus returns an empty default otherwise)."""
    if message is None:
        return False
    if isinstance(message, Mapping):
        return message.get(name) is not None
    try:
        return name in message
    except TypeError:
        pass
    has_field = getattr(type(message), "HasField", None)
    if has_field is not None:
        return bool(has_field(message, name))
    return getattr(message, name, None) is not None


def _count(value: Any) -> Optional[int]:
    """Length of a repeated field; None (never 0) when the shape is unknown."""
    if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
        return None
    try:
        return len(value)
    except TypeError:
        return None


def _replay(items: Sequence[Any], error: Exception) -> Iterator[Any]:
    """The pairs read before ``error``, then ``error``: what the vendor would have read."""
    yield from items
    raise error


def _read_metadata(
    kwargs: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], Optional[Tuple[Any, ...]]]:
    """Read the call's ``metadata`` once; return the kwargs to call with and its pairs.

    ``metadata`` may be a one-shot iterable such as a generator, so it is
    read here once and the vendor gets a tuple of the same pairs (it makes
    a tuple of it too). If reading raises, the vendor gets the pairs read so
    far and then the same error, as without the wrapper, and the pairs are
    None: the credential lookup then fails closed.
    """
    if kwargs.get("metadata") is None:
        return kwargs, ()
    items: List[Any] = []
    try:
        for item in kwargs["metadata"]:
            items.append(item)
    except Exception as error:
        return dict(kwargs, metadata=_replay(items, error)), None
    pairs = tuple(items)
    return dict(kwargs, metadata=pairs), pairs


def _credential_values(instance: Any, metadata: Optional[Sequence[Any]]) -> List[str]:
    """Every credential this call could send, longest first.

    The clients keep the google.auth credentials on the transport
    (``client._transport._credentials``; the async clients wrap a sync client
    in ``_client``) and ``client_options.api_key`` in ``_client_options``.
    A caller can also pass auth headers per call through ``metadata``, the
    pairs ``_read_metadata`` read (None if they could not be read).
    Anything that cannot be read raises: the caller then records no free
    text for the call.
    """
    if metadata is None:
        raise LookupError("the call's metadata could not be read")
    values: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in values:
            values.append(value)

    clients = [instance]
    inner = getattr(instance, "_client", None)
    if inner is not None:
        clients.append(inner)
    for client in clients:
        credentials = getattr(getattr(client, "_transport", None), "_credentials", None)
        for name in _CREDENTIAL_FIELDS:
            add(getattr(credentials, name, None))
        add(_field(getattr(client, "_client_options", None), "api_key"))

    for item in metadata:
        if not (isinstance(item, (tuple, list)) and len(item) == 2):
            continue
        key, value = item
        if isinstance(key, str) and key.lower() in _AUTH_METADATA and isinstance(value, str):
            add(value)
            parts = value.split(None, 1)
            if len(parts) == 2:
                add(parts[1])  # the token after "Bearer"
    return sorted(values, key=len, reverse=True)


def _request(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    return args[0] if args else kwargs.get("request")


def _query_text(operation: str, request: Any) -> Optional[str]:
    """``SearchRequest.query`` or ``AnswerQueryRequest.query.text``; None when empty."""
    query = _field(request, "query")
    if operation == ANSWER_QUERY:
        query = _field(query, "text")
    return query if isinstance(query, str) and query else None


def _resource_name(value: Any, secrets: Sequence[str], pii: bool) -> Optional[str]:
    if isinstance(value, str) and value and not value.endswith("/-"):
        return _clean(value, secrets, pii=pii)
    return None


def _request_attributes(
    operation: str, request: Any, secrets: Optional[Sequence[str]], options: Options
) -> Dict[str, Any]:
    pii = options.pii_redaction
    attributes: Dict[str, Any] = {FI_SPAN_KIND: RETRIEVER}
    serving_config = _resource_name(_field(request, "serving_config"), secrets or (), pii)
    if serving_config is not None:
        attributes[SERVING_CONFIG] = serving_config
    if operation == ANSWER_QUERY:
        # "sessions/-" asks the server to start a session; the response names it.
        session = _resource_name(_field(request, "session"), secrets or (), pii)
        if session is not None:
            attributes[SESSION] = session

    if options.capture_query:
        query = _query_text(operation, request)
        if query and options.hide_inputs:
            # The placeholder FITracer writes for a hidden input.value; the
            # query text never reaches the span.
            attributes[INPUT_VALUE] = REDACTED_VALUE
        elif query and secrets is not None:
            text = _clean(query, secrets, pii=pii)
            attributes[RETRIEVAL_QUERY] = text
            # The backend input panel reads input.value; keep both keys equal.
            attributes[INPUT_VALUE] = text
    return attributes


def _hides_query(options: Options) -> bool:
    return options.hide_inputs or not options.capture_query


def _hidden_inputs(
    operation: str, request: Any, secrets: Optional[Sequence[str]], options: Options
) -> Tuple[str, ...]:
    """The query text to remove from server-written text, as it reads after scrubbing.

    Empty when the query is recorded (``capture_query`` without
    ``hide_inputs``) or empty.
    """
    if not _hides_query(options):
        return ()
    query = _query_text(operation, request)
    if not query:
        return ()
    text = _scrub(query, secrets or (), server=True)
    return (text,) if text else ()


def _response_attributes(
    operation: str, result: Any, secrets: Sequence[str], options: Options
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Counts, the answer state and the session; never result or answer text.

    Returns the attributes and, for an answer whose state is FAILED, the
    error status description.
    """
    attributes: Dict[str, Any] = {}
    if operation != ANSWER_QUERY:
        # The pager reads through to the first SearchResponse.
        results = _count(_field(result, "results"))
        if results is not None:
            attributes[RESULT_COUNT] = results
        return attributes, None

    failure = None
    if _has(result, "answer"):
        answer = _field(result, "answer")
        references = _count(_field(answer, "references"))
        if references is not None:
            attributes[RESULT_COUNT] = references
        text = _field(answer, "answer_text")
        if isinstance(text, str):
            attributes[ANSWER_LENGTH] = len(text)
        state = getattr(_field(answer, "state"), "name", None)
        if isinstance(state, str):
            attributes[ANSWER_STATE] = state
            if state == _FAILED_STATE:
                failure = "answer state FAILED"
    if _has(result, "session"):
        session = _resource_name(
            _field(_field(result, "session"), "name"), secrets, options.pii_redaction
        )
        if session is not None:
            attributes[SESSION] = session
    return attributes, failure


def _describe(error: BaseException) -> str:
    try:
        return str(error)
    except Exception:
        return "<unprintable {0}>".format(type(error).__name__)


def _error_attributes(error: BaseException) -> Dict[str, Any]:
    """The gRPC status name and HTTP-style code of a ``google.api_core`` error.

    A ``RetryError`` has neither; the last attempt's error (``cause``) does.
    """
    source: Any = error
    if getattr(error, "grpc_status_code", None) is None and getattr(error, "cause", None) is not None:
        source = error.cause  # type: ignore[attr-defined]
    attributes: Dict[str, Any] = {}
    status = getattr(getattr(source, "grpc_status_code", None), "name", None)
    if isinstance(status, str):
        attributes[ERROR_STATUS] = status
    code = getattr(source, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        attributes[ERROR_CODE] = int(code)
    return attributes


def _exception_attributes(
    error: BaseException,
    secrets: Optional[Sequence[str]],
    pii: bool = False,
    hidden: Hidden = (),
) -> Dict[str, Any]:
    """The OTel exception event, with credentials removed from every text.

    The hidden query and then PII are replaced next when enabled. The
    message is then cut to 1 KB and the stacktrace to 16 KB of UTF-8: a
    server error can echo a large request into both. Without readable
    credentials only the type is kept.
    """
    error_type = type(error)
    module = error_type.__module__
    qualified = (
        "{0}.{1}".format(module, error_type.__qualname__)
        if module and module != "builtins"
        else error_type.__qualname__
    )
    if secrets is None:
        return {"exception.type": qualified, "exception.message": UNREADABLE}
    stacktrace = "".join(traceback.format_exception(error_type, error, error.__traceback__))
    return {
        "exception.type": qualified,
        "exception.message": _clean(_describe(error), secrets, pii=pii, hidden=hidden, server=True),
        "exception.stacktrace": _clean(
            stacktrace, secrets, MAX_STACKTRACE_BYTES, pii, hidden, server=True
        ),
    }


@contextlib.contextmanager
def _current(span: Span) -> Iterator[None]:
    """Make ``span`` current for the vendor call so gRPC client spans nest under it.

    Also marks the call active, so a wrapped method called inside it opens
    no second span. The span is ended by the wrapper, not on exit, and
    errors are recorded once by ``_Call.error``. A failure to attach the
    context leaves the call running without it rather than raising into the
    caller.
    """
    manager = None
    token = None
    try:
        token = _ACTIVE.set(True)
        manager = trace_api.use_span(
            span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        )
        manager.__enter__()
    except Exception:
        logger.debug("Could not make the Discovery Engine span current", exc_info=True)
        manager = None
    try:
        yield
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                logger.debug("Could not restore the trace context", exc_info=True)
        if token is not None:
            try:
                _ACTIVE.reset(token)
            except Exception:
                logger.debug("Could not reset the active-call marker", exc_info=True)


class _Call:
    """One traced Discovery Engine call. Every method is isolated and ends the span once."""

    def __init__(
        self,
        span: Span,
        operation: str,
        secrets: Optional[List[str]],
        options: Options,
        hidden: Hidden = (),
        instance: Any = None,
        metadata: Optional[Tuple[Any, ...]] = (),
    ) -> None:
        self.span = span
        self.operation = operation
        self.secrets = secrets
        self.options = options
        self.hidden = hidden
        self.instance = instance
        self.metadata = metadata

    def ok(self, result: Any) -> None:
        failure = None
        try:
            attributes, failure = _response_attributes(
                self.operation, result, self.secrets or [], self.options
            )
            for key, value in attributes.items():
                self.span.set_attribute(key, value)
        except Exception:
            logger.debug("Could not read the Discovery Engine response", exc_info=True)
        try:
            if failure is None:
                self.span.set_status(Status(StatusCode.OK))
            else:
                # Returned, not raised: no exception event.
                self.span.set_status(Status(StatusCode.ERROR, failure))
        except Exception:
            logger.debug("Could not set the span status", exc_info=True)
        self._end()

    def _secrets_after_call(self) -> Optional[List[str]]:
        """The credentials read before the call and any set during it, longest first.

        A token minted during the call (first use or a refresh) is set on
        the credentials the client holds after they were first read, so
        they are read again (attribute reads only; nothing is refreshed).
        None, failing closed, if either read failed.
        """
        if self.secrets is None:
            return None
        try:
            current = _credential_values(self.instance, self.metadata)
        except Exception:
            logger.debug("Could not read the Discovery Engine credentials again", exc_info=True)
            return None
        return sorted(dict.fromkeys(self.secrets + current), key=len, reverse=True)

    def error(self, error: BaseException) -> None:
        # No result count: nothing was returned, so the count is unknown.
        secrets = self._secrets_after_call()
        try:
            if secrets is None:
                message = UNREADABLE
            else:
                message = _clean(
                    _describe(error),
                    secrets,
                    pii=self.options.pii_redaction,
                    hidden=self.hidden,
                    server=True,
                )
            self.span.set_status(
                Status(StatusCode.ERROR, "{0}: {1}".format(type(error).__name__, message))
            )
        except Exception:
            logger.debug("Could not set the error status", exc_info=True)
        try:
            for key, value in _error_attributes(error).items():
                self.span.set_attribute(key, value)
        except Exception:
            logger.debug("Could not read the error status code", exc_info=True)
        try:
            self.span.add_event(
                "exception",
                _exception_attributes(error, secrets, self.options.pii_redaction, self.hidden),
            )
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
            logger.debug("Could not end the Discovery Engine span", exc_info=True)


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
        self._span_name = "discoveryengine.{0}".format(operation)
        self._options = options
        self._state = state or _State()

    def _start(
        self, instance: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]
    ) -> Tuple[Optional[_Call], Mapping[str, Any]]:
        """Start the span (None to run the call untraced); return it and the kwargs to call with.

        The kwargs differ from the caller's only in ``metadata``, read once
        by ``_read_metadata``.
        """
        if not self._state.enabled or _ACTIVE.get():
            return None, kwargs
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return None, kwargs
        request = _request(args, kwargs)
        kwargs, metadata = _read_metadata(kwargs)
        secrets: Optional[List[str]]
        try:
            secrets = _credential_values(instance, metadata)
        except Exception:
            # Fail closed: without the credentials, no free text is recorded.
            logger.debug("Could not read the Discovery Engine credentials", exc_info=True)
            secrets = None
        try:
            attributes = _request_attributes(self._operation, request, secrets, self._options)
        except Exception:  # an attribute must never break the user's call
            logger.debug("Could not read the Discovery Engine request", exc_info=True)
            attributes = {FI_SPAN_KIND: RETRIEVER}
        hidden: Hidden = ()
        try:
            hidden = _hidden_inputs(self._operation, request, secrets, self._options)
        except Exception:
            # The query must be hidden but is unreadable: keep no server text.
            logger.debug("Could not read the Discovery Engine query to hide", exc_info=True)
            hidden = None if _hides_query(self._options) else ()
        try:
            span = self._tracer.start_span(self._span_name, attributes=attributes)
        except Exception:
            logger.debug("Could not start the Discovery Engine span", exc_info=True)
            return None, kwargs
        call = _Call(span, self._operation, secrets, self._options, hidden, instance, metadata)
        return call, kwargs


class OperationWrapper(_BaseWrapper):
    """Trace a synchronous ``search`` / ``search_lite`` / ``answer_query`` call."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        call, kwargs = self._start(instance, args, kwargs)
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
    """Trace an async ``search`` / ``search_lite`` / ``answer_query`` call."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        call, kwargs = self._start(instance, args, kwargs)
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
