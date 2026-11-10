"""Span wrapper for ``TypeSafeClient.system_one`` and ``AsyncTypeSafeClient.system_one``.

One ``EVALUATOR`` span per public call, however many questions the request
asks. Each question is a set of ``typesafe.question.<name>.*`` attributes, not
a child span: TypeSafe answers every question in one HTTP response.

What a span records (traceAI ``TraceConfig`` convention, content on by default):

* Always: provider ``typesafe``, request model, response model, server host,
  question names and types, the numeric answers (``noul`` probability,
  ``score``, ``confidence``, box edges), token usage and the request id.
* Unless ``hide_inputs`` / ``hide_input_text``: ``input.value`` with the
  ``state`` and the questions (instructions and criteria), and an
  ``Idempotency-Key`` you sent.
* Unless ``hide_outputs`` / ``hide_output_text``: the chosen ``choice`` label
  and ``output.value`` with every decision.
* Never: probability maps, score legends, image bytes, the API key, an
  explanation (the API returns none), a Pass/Fail mapping of ``noul``, stack
  traces, or the text of client-side errors (they can carry local file paths).

Nothing in this module may change what the caller gets back or raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode, Tracer

try:  # TraceConfig.mask applies this to attributes when pii_redaction is on.
    from fi_instrumentation.instrumentation.config import redact_pii_in_value
except ImportError:  # pragma: no cover - present in fi-instrumentation-otel >= 1.1.0
    redact_pii_in_value = None

try:
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled
except ImportError:  # pragma: no cover - older opentelemetry-instrumentation
    def is_instrumentation_enabled() -> bool:
        return not context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY)


logger = logging.getLogger(__name__)

PROVIDER = "typesafe"
SPAN_NAME = "typesafe.system_one"

# Attribute names. Question names are the caller's own keys.
QUESTION_PREFIX = "typesafe.question."
QUESTION_COUNT = "typesafe.question_count"
RESPONSE_MODEL = "typesafe.response.model"
REQUEST_ID = "typesafe.request_id"
IDEMPOTENCY_KEY = "typesafe.idempotency_key"
SERVER_ERROR_TYPE = "typesafe.error_type"
RETRY_AFTER_MS = "typesafe.retry_after_ms"
RESPONSE_FIELD_PATH = "typesafe.response.field_path"
CANCELLED = "typesafe.cancelled"
SERVER_ADDRESS = "server.address"
HTTP_STATUS_CODE = "http.response.status_code"

JSON_MIME_TYPE = "application/json"
TEXT_MIME_TYPE = "text/plain"
REDACTED = "[redacted]"

# The SDK sends at most what the API accepts; these caps only bound what a
# span carries. Counts stay exact.
MAX_QUESTIONS = 64
MAX_CAPTURED_CHARS = 32768
MAX_ERROR_MESSAGE_CHARS = 1024
_MAX_DEPTH = 32

_ANSWER_TYPES = ("noul", "choice", "score", "box")
_BOX_EDGES = ("top", "bottom", "left", "right")
_IMAGE_REFERENCE = re.compile(r"<image:[0-9a-f]{32}>")

# A Ctrl-C during a blocking call, or a cancelled asyncio task, is a
# cancellation, not a failure of the call.
_CANCELLATIONS = (asyncio.CancelledError, KeyboardInterrupt)

_MISSING = object()


# --------------------------------------------------------------------------- #
# Reading the call
# --------------------------------------------------------------------------- #


def _argument(args: Sequence[Any], kwargs: Mapping[str, Any], name: str, index: int) -> Any:
    """``state`` and ``questions`` may be positional; every other argument is keyword-only."""
    if name in kwargs:
        return kwargs[name]
    return args[index] if index < len(args) else _MISSING


def _config_value(instance: Any, name: str) -> Any:
    """A resolved client setting (``Config`` is internal to typesafe-sdk, so read defensively)."""
    try:
        return getattr(getattr(instance, "_config", None), name, None)
    except Exception:
        return None


def _secrets(instance: Any) -> List[str]:
    """The API key the client sends. Never exported, wherever it appears."""
    key = _config_value(instance, "api_key")
    return [key] if isinstance(key, str) and key else []


def _redact(value: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    return value


def _request_model(instance: Any, kwargs: Mapping[str, Any]) -> Optional[str]:
    """The model the request asks for, as the SDK resolves it.

    ``extra_body["model"]`` overrides the body last, then ``model=``, then
    the client default (``model=`` on the client, ``TYPESAFE_DEFAULT_MODEL``
    or the SDK default).
    """
    extra_body = kwargs.get("extra_body")
    if isinstance(extra_body, Mapping):
        override = extra_body.get("model")
        if isinstance(override, str) and override:
            return override
    model = kwargs.get("model")
    if isinstance(model, str) and model:
        return model
    default = _config_value(instance, "default_model")
    return default if isinstance(default, str) and default else None


def _server_address(instance: Any) -> Optional[str]:
    """Host of the base URL the client uses. The instrumentor never sets it."""
    base_url = _config_value(instance, "base_url")
    if not isinstance(base_url, str) or not base_url:
        return None
    try:
        return urlsplit(base_url).hostname or None
    except ValueError:
        return None


def _field(value: Any, name: str) -> Any:
    """A field of an SDK object or of a raw dictionary."""
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _question_type(question: Any) -> Optional[str]:
    kind = _field(question, "type")
    return kind if isinstance(kind, str) and kind else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _question_names(questions: Any) -> List[str]:
    if not isinstance(questions, Mapping):
        return []
    return [name for name in questions if isinstance(name, str)]


def _idempotency_key(kwargs: Mapping[str, Any]) -> Optional[str]:
    headers = kwargs.get("extra_headers")
    if not isinstance(headers, Mapping):
        return None
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == "idempotency-key" and isinstance(value, str):
            return value
    return None


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #


def _jsonable(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy of request content.

    Images become their opaque ``<image:...>`` reference (never bytes or a
    file path); any other unknown object becomes ``<TypeName>``.
    """
    if depth > _MAX_DEPTH:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<bytes>"
    fields = getattr(type(value), "model_fields", None)
    if isinstance(fields, Mapping):  # a typesafe_sdk question object (pydantic)
        dumped = {}
        for name in fields:
            item = getattr(value, name, None)
            if item is not None:
                dumped[name] = _jsonable(item, depth + 1)
        return dumped
    try:
        text = str(value)
    except Exception:
        text = ""
    if _IMAGE_REFERENCE.fullmatch(text):
        return text
    return "<{0}>".format(type(value).__name__)


def _dump(payload: Any, secrets: Sequence[str]) -> Tuple[str, str]:
    """JSON text of ``payload``, key removed, capped at MAX_CAPTURED_CHARS."""
    text = _redact(json.dumps(payload, ensure_ascii=False, default=str), secrets)
    if len(text) <= MAX_CAPTURED_CHARS:
        return text, JSON_MIME_TYPE
    return text[:MAX_CAPTURED_CHARS] + "...[truncated]", TEXT_MIME_TYPE


def _hides_inputs(config: TraceConfig) -> bool:
    return bool(config.hide_inputs or config.hide_input_text)


def _hides_outputs(config: TraceConfig) -> bool:
    return bool(config.hide_outputs or config.hide_output_text)


def _input_content(
    args: Sequence[Any], kwargs: Mapping[str, Any], secrets: Sequence[str], config: TraceConfig
) -> Dict[str, Any]:
    if _hides_inputs(config):
        return {}
    payload: Dict[str, Any] = {}
    state = _argument(args, kwargs, "state", 0)
    if state is not _MISSING:
        payload["state"] = _jsonable(state)
    questions = _argument(args, kwargs, "questions", 1)
    if questions is not _MISSING:
        payload["questions"] = _jsonable(questions)
    attributes: Dict[str, Any] = {}
    if payload:
        text, mime = _dump(payload, secrets)
        attributes[SpanAttributes.INPUT_VALUE] = text
        attributes[SpanAttributes.INPUT_MIME_TYPE] = mime
    key = _idempotency_key(kwargs)
    if key:
        attributes[IDEMPOTENCY_KEY] = _redact(key, secrets)
    return attributes


# --------------------------------------------------------------------------- #
# Attributes
# --------------------------------------------------------------------------- #


def _request_attributes(
    instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> Dict[str, Any]:
    """Known before the call. Never content."""
    attributes: Dict[str, Any] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.EVALUATOR.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: PROVIDER,
    }
    model = _request_model(instance, kwargs)
    if model:
        attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
    host = _server_address(instance)
    if host:
        attributes[SERVER_ADDRESS] = host
    questions = _argument(args, kwargs, "questions", 1)
    names = _question_names(questions)
    if isinstance(questions, Mapping):
        attributes[QUESTION_COUNT] = len(questions)
        for name in names[:MAX_QUESTIONS]:
            kind = _question_type(questions[name])
            if kind:
                attributes["{0}{1}.type".format(QUESTION_PREFIX, name)] = kind
    return attributes


def _answers(result: Any) -> Iterable[Tuple[str, Any]]:
    answers = _field(result, "answers")
    if not isinstance(answers, Mapping):
        return ()
    return [(name, answer) for name, answer in answers.items() if isinstance(name, str)]


def _answer_attributes(name: str, answer: Any, hide_outputs: bool) -> Dict[str, Any]:
    """The decision for one question. No probability map, no legend, no explanation."""
    prefix = "{0}{1}.".format(QUESTION_PREFIX, name)
    attributes: Dict[str, Any] = {}
    kind = _question_type(answer)
    if kind:
        attributes[prefix + "type"] = kind
    if kind == "choice":
        choice = _field(answer, "choice")
        if isinstance(choice, str) and not hide_outputs:
            attributes[prefix + "choice"] = choice
    elif kind in ("score", "noul"):
        value = _number(_field(answer, kind))
        if value is not None:
            attributes[prefix + kind] = value
    elif kind == "box":
        box = _field(answer, "box")
        for edge in _BOX_EDGES:
            value = _number(_field(box, edge))
            if value is not None:
                attributes["{0}box.{1}".format(prefix, edge)] = value
    # Recorded only when the API sent one: noul answers have no confidence and
    # box confidence may be null. Never invented.
    confidence = _number(_field(answer, "confidence"))
    if confidence is not None:
        attributes[prefix + "confidence"] = confidence
    return attributes


def _decision(answer: Any) -> Dict[str, Any]:
    kind = _question_type(answer)
    decision: Dict[str, Any] = {"type": kind}
    if kind == "choice":
        decision["choice"] = _field(answer, "choice")
    elif kind in ("score", "noul"):
        decision[kind] = _number(_field(answer, kind))
    elif kind == "box":
        box = _field(answer, "box")
        decision["box"] = {edge: _number(_field(box, edge)) for edge in _BOX_EDGES}
    confidence = _number(_field(answer, "confidence"))
    if confidence is not None:
        decision["confidence"] = confidence
    return decision


def _usage_attributes(result: Any) -> Dict[str, Any]:
    """Token counts as reported. Never estimated, never priced."""
    usage = _field(result, "usage")
    attributes: Dict[str, Any] = {}
    counts = []
    for field, key in (
        ("input_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
        ("output_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
    ):
        value = _field(usage, field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            attributes[key] = value
            counts.append(value)
    if len(counts) == 2:
        attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = sum(counts)
    return attributes


def _request_id(result: Any) -> Optional[str]:
    try:  # SystemOneResponse.request_id raises when the header is absent
        value = getattr(result, "request_id", None)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _response_attributes(
    result: Any, request_model: Optional[str], config: TraceConfig
) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {}
    model = _field(result, "model")
    if isinstance(model, str) and model:
        attributes[SpanAttributes.GEN_AI_RESPONSE_MODEL] = model
        if model != request_model:
            # An alias resolved to a version, or a gateway's own id. The
            # request model stays as it was asked for.
            attributes[RESPONSE_MODEL] = model
    attributes.update(_usage_attributes(result))
    request_id = _request_id(result)
    if request_id:
        attributes[REQUEST_ID] = request_id
    hide_outputs = _hides_outputs(config)
    answers = list(_answers(result))
    for name, answer in answers[:MAX_QUESTIONS]:
        attributes.update(_answer_attributes(name, answer, hide_outputs))
    return attributes


def _output_content(
    result: Any, secrets: Sequence[str], config: TraceConfig
) -> Dict[str, Any]:
    if _hides_outputs(config):
        return {}
    answers = list(_answers(result))
    if not answers:
        return {}
    text, mime = _dump(
        {name: _decision(answer) for name, answer in answers[:MAX_QUESTIONS]}, secrets
    )
    return {SpanAttributes.OUTPUT_VALUE: text, SpanAttributes.OUTPUT_MIME_TYPE: mime}


def _set_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        try:
            span.set_attribute(key, value)
        except Exception:
            logger.debug("Could not set %s on the TypeSafe span", key, exc_info=True)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def _server_message(error: BaseException) -> Optional[str]:
    """The API's own error message, without the endpoint prefix.

    Only for HTTP errors (``TypeSafeAPIError`` has ``status``). Client-side
    ``TypeSafeError`` text can name a local file, so it is never used.
    """
    try:
        message = str(error)
    except Exception:
        return None
    # str(TypeSafeAPIError) is "<METHOD URL>: <status> <message> (request_id=<id>)";
    # the endpoint, status and request id are recorded as their own fields.
    endpoint = getattr(error, "endpoint", None)
    if isinstance(endpoint, str) and endpoint and message.startswith(endpoint + ": "):
        message = message[len(endpoint) + 2 :]
    status = getattr(error, "status", None)
    if isinstance(status, int) and message.startswith("{0} ".format(status)):
        message = message[len(str(status)) + 1 :]
    try:
        request_id = getattr(error, "request_id", None)
    except Exception:
        request_id = None
    suffix = " (request_id={0})".format(request_id)
    if isinstance(request_id, str) and message.endswith(suffix):
        message = message[: -len(suffix)]
    return message.strip() or None


def _redact_pii(value: str, config: TraceConfig) -> str:
    """FiSpan masks attributes only; status and events are redacted here."""
    if not config.pii_redaction or redact_pii_in_value is None:
        return value
    redacted = redact_pii_in_value(value)
    return redacted if isinstance(redacted, str) else value


def _error_attributes(error: BaseException) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {SpanAttributes.ERROR_TYPE: type(error).__name__}
    status = getattr(error, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        attributes[HTTP_STATUS_CODE] = status
    try:
        request_id = getattr(error, "request_id", None)
    except Exception:
        request_id = None
    if isinstance(request_id, str) and request_id:
        attributes[REQUEST_ID] = request_id
    body = getattr(error, "body", None)
    detail = body.get("detail") if isinstance(body, Mapping) else None
    server_type = detail.get("error_type") if isinstance(detail, Mapping) else None
    if isinstance(server_type, str) and server_type:
        attributes[SERVER_ERROR_TYPE] = server_type
    retry_after = _number(getattr(error, "retry_after_ms", None))
    if retry_after is not None:
        attributes[RETRY_AFTER_MS] = retry_after
    field_path = getattr(error, "field_path", None)
    if isinstance(field_path, str) and field_path:
        attributes[RESPONSE_FIELD_PATH] = field_path
    return attributes


def _record_error(
    span: Span, error: BaseException, secrets: Sequence[str], config: TraceConfig
) -> None:
    """ERROR status, structured fields and an ``exception`` event without a stack trace.

    The server's message is added only while content is captured (neither
    inputs nor outputs hidden), since an API can echo request values.
    """
    error_class = type(error)
    attributes = _error_attributes(error)
    _set_attributes(span, attributes)
    description = error_class.__name__
    status = attributes.get(HTTP_STATUS_CODE)
    if status is not None:
        description = "{0} (HTTP {1})".format(description, status)
    event: Dict[str, Any] = {
        "exception.type": "{0}.{1}".format(error_class.__module__, error_class.__qualname__)
    }
    if status is not None and not _hides_inputs(config) and not _hides_outputs(config):
        message = _server_message(error)
        if message:
            message = _redact_pii(_redact(message, secrets), config)[:MAX_ERROR_MESSAGE_CHARS]
            event["exception.message"] = message
            description = "{0}: {1}".format(description, message)
    span.add_event("exception", event)
    span.set_status(Status(StatusCode.ERROR, description))


def _end(span: Span) -> None:
    try:
        span.end()
    except Exception:
        logger.debug("Could not end the TypeSafe span", exc_info=True)


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #


class _Call:
    """Tracing state for one ``system_one`` call. Every step swallows its own errors."""

    __slots__ = ("_config", "_span", "_secrets", "_token", "_model")

    def __init__(
        self,
        tracer: Tracer,
        config: TraceConfig,
        instance: Any,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> None:
        self._config = config
        self._span: Optional[Span] = None
        self._token: Any = None
        self._secrets: List[str] = []
        self._model: Optional[str] = None
        try:
            self._secrets = _secrets(instance)
        except Exception:
            logger.debug("Could not read the TypeSafe client's key", exc_info=True)
        attributes: Dict[str, Any] = {}
        try:
            attributes.update(_request_attributes(instance, args, kwargs))
            self._model = attributes.get(SpanAttributes.GEN_AI_REQUEST_MODEL)
        except Exception:
            logger.debug("Could not read TypeSafe request attributes", exc_info=True)
        try:
            attributes.update(_input_content(args, kwargs, self._secrets, config))
        except Exception:
            logger.debug("Could not capture TypeSafe request content", exc_info=True)
        try:
            self._span = tracer.start_span(
                SPAN_NAME,
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            )
        except Exception:
            logger.debug("Could not start the TypeSafe span", exc_info=True)
            self._span = None

    def enter(self) -> None:
        if self._span is None:
            return
        try:
            self._token = context_api.attach(trace_api.set_span_in_context(self._span))
        except Exception:
            self._token = None

    def exit(self) -> None:
        if self._token is None:
            return
        try:
            context_api.detach(self._token)
        except Exception:
            logger.debug("Could not restore the trace context", exc_info=True)
        self._token = None

    def ok(self, result: Any) -> None:
        span = self._span
        if span is None:
            return
        try:
            try:
                _set_attributes(span, _response_attributes(result, self._model, self._config))
            except Exception:
                logger.debug("Could not read the TypeSafe response", exc_info=True)
            try:
                _set_attributes(span, _output_content(result, self._secrets, self._config))
            except Exception:
                logger.debug("Could not capture the TypeSafe response content", exc_info=True)
            span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("Could not finish the TypeSafe span", exc_info=True)
        finally:
            _end(span)

    def error(self, error: BaseException) -> None:
        span = self._span
        if span is None:
            return
        try:
            _record_error(span, error, self._secrets, self._config)
        except Exception:
            logger.debug("Could not record the TypeSafe error", exc_info=True)
        finally:
            _end(span)

    def cancelled(self) -> None:
        span = self._span
        if span is None:
            return
        try:
            span.set_attribute(CANCELLED, True)
            span.set_status(Status(StatusCode.ERROR, "cancelled"))
        except Exception:
            logger.debug("Could not mark the TypeSafe span cancelled", exc_info=True)
        finally:
            _end(span)


class _BaseWrapper:
    def __init__(self, tracer: Tracer, config: TraceConfig) -> None:
        self._tracer = tracer
        self._config = config

    def _begin(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> Optional[_Call]:
        try:
            if not is_instrumentation_enabled():
                return None
            return _Call(self._tracer, self._config, instance, args, kwargs)
        except Exception:
            logger.debug("Could not trace the TypeSafe call", exc_info=True)
            return None


class SyncSystemOneWrapper(_BaseWrapper):
    """Trace ``TypeSafeClient.system_one``."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        call = self._begin(instance, args, kwargs)
        if call is None:
            return wrapped(*args, **kwargs)
        call.enter()
        try:
            result = wrapped(*args, **kwargs)
        except _CANCELLATIONS:
            call.exit()
            call.cancelled()
            raise
        except BaseException as error:
            call.exit()
            call.error(error)
            raise
        call.exit()
        call.ok(result)
        return result


class AsyncSystemOneWrapper(_BaseWrapper):
    """Trace ``AsyncTypeSafeClient.system_one``."""

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        call = self._begin(instance, args, kwargs)
        if call is None:
            return await wrapped(*args, **kwargs)
        call.enter()
        try:
            result = await wrapped(*args, **kwargs)
        except _CANCELLATIONS:
            call.exit()
            call.cancelled()
            raise
        except BaseException as error:
            call.exit()
            call.error(error)
            raise
        call.exit()
        call.ok(result)
        return result
