"""Span wrappers for voyageai ``Client`` / ``AsyncClient`` ``embed`` and ``rerank``.

One span per public call. The span is current while the SDK sends HTTP, so an
HTTP client instrumentation nests under it, and it covers the client's own
retries. By default the rerank span records the query and the scores (PRD J2,
AC-03); embed texts and rerank documents are recorded only with
``capture_content=True``. TraceConfig ``hide_inputs`` / ``hide_outputs`` drop
them. Embedding vectors are never recorded. Nothing in this module may change
what the caller gets back or raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, RerankerAttributes, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode, Tracer

try:  # The function TraceConfig.mask applies when pii_redaction is on.
    from fi_instrumentation.instrumentation.config import redact_pii_in_value
except ImportError:  # pragma: no cover - present from fi-instrumentation-otel 1.1.0
    redact_pii_in_value = None

logger = logging.getLogger(__name__)

PROVIDER = "voyage"
REDACTED = "[redacted]"
CANCELLED = "voyage.cancelled"
INPUT_TYPE = "voyage.input_type"
OUTPUT_DTYPE = "voyage.output_dtype"
OUTPUT_DIMENSION = "voyage.output_dimension"
EMBEDDING_COUNT = "voyage.embedding.count"
EMBEDDING_DIMENSION = "voyage.embedding.dimension"
RERANK_DOCUMENT_COUNT = "voyage.rerank.document_count"
RERANK_RESULT_COUNT = "voyage.rerank.result_count"
SERVER_ADDRESS = "server.address"
JSON_MIME_TYPE = "application/json"
TEXT_MIME_TYPE = "text/plain"

# Recorded content limits: at most this many texts, documents or rerank
# scores, and each text, document or query cut to this many UTF-8 bytes on a
# character boundary. Counts stay exact.
MAX_CAPTURED_ITEMS = 64
MAX_CAPTURED_BYTES = 2048

# binary / ubinary pack eight dimensions into each returned integer.
_PACKED_DTYPES = ("binary", "ubinary")

# A Ctrl-C during a blocking call, or a cancelled asyncio task, is a
# cancellation rather than a failure of the call.
_CANCELLATIONS = (asyncio.CancelledError, KeyboardInterrupt)

_EMBED_PARAMETERS = (
    "texts",
    "model",
    "input_type",
    "truncation",
    "output_dtype",
    "output_dimension",
)
_RERANK_PARAMETERS = ("query", "documents", "model", "top_k", "truncation")


@dataclass(frozen=True)
class Operation:
    """What one wrapped method records."""

    span_name: str
    span_kind: str
    operation_name: str
    parameters: Tuple[str, ...]


EMBED = Operation(
    span_name="voyage.embed",
    span_kind=FiSpanKindValues.EMBEDDING.value,
    operation_name="embeddings",
    parameters=_EMBED_PARAMETERS,
)
RERANK = Operation(
    span_name="voyage.rerank",
    span_kind=FiSpanKindValues.RERANKER.value,
    operation_name="rerank",
    parameters=_RERANK_PARAMETERS,
)


def _argument(
    operation: Operation, args: Sequence[Any], kwargs: Mapping[str, Any], name: str
) -> Any:
    """Read a vendor argument passed by keyword or by position (``self`` excluded)."""
    if name in kwargs:
        return kwargs[name]
    index = operation.parameters.index(name)
    return args[index] if index < len(args) else None


def _voyage_module() -> Any:
    return sys.modules.get("voyageai")


def _secrets(instance: Any) -> List[str]:
    """Every API key the client could send: never exported, wherever it appears.

    voyageai keeps the key on ``client.api_key`` and ``client._params["api_key"]``
    (read from the argument, ``voyageai.api_key``, ``VOYAGE_API_KEY`` or a key
    file); a module-level ``voyageai.api_key`` is included too.
    """
    candidates: List[Any] = [getattr(instance, "api_key", None)]
    params = getattr(instance, "_params", None)
    if isinstance(params, Mapping):
        candidates.append(params.get("api_key"))
    module = _voyage_module()
    if module is not None:
        candidates.append(getattr(module, "api_key", None))
    secrets: List[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in secrets:
            secrets.append(candidate)
    # Longest first, so a key that contains another is removed whole.
    return sorted(secrets, key=len, reverse=True)


def _redact(value: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    return value


def _cap(value: str) -> str:
    """The longest whole-character prefix of at most MAX_CAPTURED_BYTES of UTF-8."""
    return value.encode("utf-8", "replace")[:MAX_CAPTURED_BYTES].decode("utf-8", "ignore")


def _captured_text(value: Any, secrets: Sequence[str]) -> str:
    return _cap(_redact(value if isinstance(value, str) else str(value), secrets))


def _captured_list(values: Any, secrets: Sequence[str]) -> Optional[List[str]]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return None
    return [_captured_text(value, secrets) for value in values[:MAX_CAPTURED_ITEMS]]


def _count(values: Any) -> Optional[int]:
    """Number of texts or documents; a bare string is one. None when unknown."""
    if isinstance(values, str):
        return 1
    if isinstance(values, (list, tuple)):
        return len(values)
    return None


def _model(operation: Operation, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Optional[str]:
    model = _argument(operation, args, kwargs, "model")
    if model is None and operation is EMBED:
        # Client.embed falls back to this default (and warns) when model is None.
        model = getattr(_voyage_module(), "VOYAGE_EMBED_DEFAULT_MODEL", None)
    return model if isinstance(model, str) and model else None


_UNRESOLVED = object()
_local_model_check: Any = _UNRESOLVED


def _is_local_model(model: Optional[str]) -> bool:
    """voyageai >= 0.5 runs some embedding models in process, with no request."""
    global _local_model_check
    if not model:
        return False
    if _local_model_check is _UNRESOLVED:
        # Resolved once: a failed import is not cached by Python, and older
        # releases would otherwise search sys.path on every call.
        try:
            from voyageai.local.helpers import is_local_model
        except Exception:  # voyageai < 0.5 has no local models
            is_local_model = None
        _local_model_check = is_local_model
    if _local_model_check is None:
        return False
    try:
        return bool(_local_model_check(model))
    except Exception:
        return False


def _server_address(instance: Any) -> Optional[str]:
    """The host the client chose (from its key or the caller's base_url), never a URL."""
    params = getattr(instance, "_params", None)
    url = params.get("base_url") if isinstance(params, Mapping) else None
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


def _base_attributes(operation: Operation) -> Dict[str, Any]:
    return {
        SpanAttributes.GEN_AI_SPAN_KIND: operation.span_kind,
        SpanAttributes.GEN_AI_PROVIDER_NAME: PROVIDER,
        SpanAttributes.GEN_AI_OPERATION_NAME: operation.operation_name,
    }


def _request_attributes(
    operation: Operation, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> Dict[str, Any]:
    """Attributes known before the call. Never content."""
    attributes: Dict[str, Any] = {}
    model = _model(operation, args, kwargs)
    if model:
        attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
    if operation is EMBED:
        if model:
            attributes[SpanAttributes.EMBEDDING_MODEL_NAME] = model
        count = _count(_argument(operation, args, kwargs, "texts"))
        if count is not None:
            attributes[EMBEDDING_COUNT] = count
        for name, key in (("input_type", INPUT_TYPE), ("output_dtype", OUTPUT_DTYPE)):
            value = _argument(operation, args, kwargs, name)
            if isinstance(value, str) and value:
                attributes[key] = value
        dimension = _argument(operation, args, kwargs, "output_dimension")
        if isinstance(dimension, int) and not isinstance(dimension, bool):
            attributes[OUTPUT_DIMENSION] = dimension
    else:
        if model:
            attributes[RerankerAttributes.RERANKER_MODEL_NAME] = model
        count = _count(_argument(operation, args, kwargs, "documents"))
        if count is not None:
            attributes[RERANK_DOCUMENT_COUNT] = count
        top_k = _argument(operation, args, kwargs, "top_k")
        if isinstance(top_k, int) and not isinstance(top_k, bool):
            attributes[RerankerAttributes.RERANKER_TOP_K] = top_k
    if not (operation is EMBED and _is_local_model(model)):
        host = _server_address(instance)
        if host:
            attributes[SERVER_ADDRESS] = host
    return attributes


def _hides_inputs(config: TraceConfig) -> bool:
    return bool(config.hide_inputs or config.hide_input_text)


def _input_content(
    operation: Operation,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    secrets: Sequence[str],
    config: TraceConfig,
    capture_content: bool,
) -> Dict[str, Any]:
    """Input content, unless TraceConfig hides inputs.

    By default only the rerank query (PRD J2, AC-03): ``reranker.query`` and a
    plain-text ``input.value``. With ``capture_content`` the embed texts, and
    the rerank query with its documents as JSON in ``input.value``.
    """
    if _hides_inputs(config):
        return {}
    attributes: Dict[str, Any] = {}
    if operation is EMBED:
        if not capture_content:
            return attributes
        texts = _captured_list(_argument(operation, args, kwargs, "texts"), secrets)
        if texts is not None:
            attributes[SpanAttributes.INPUT_VALUE] = json.dumps(texts, ensure_ascii=False)
            attributes[SpanAttributes.INPUT_MIME_TYPE] = JSON_MIME_TYPE
        return attributes
    query = _argument(operation, args, kwargs, "query")
    if query is not None:
        attributes[RerankerAttributes.RERANKER_QUERY] = _captured_text(query, secrets)
    if not capture_content:
        if query is not None:
            attributes[SpanAttributes.INPUT_VALUE] = attributes[RerankerAttributes.RERANKER_QUERY]
            attributes[SpanAttributes.INPUT_MIME_TYPE] = TEXT_MIME_TYPE
        return attributes
    documents = _captured_list(_argument(operation, args, kwargs, "documents"), secrets)
    payload: Dict[str, Any] = {}
    if query is not None:
        payload["query"] = attributes[RerankerAttributes.RERANKER_QUERY]
    if documents is not None:
        payload["documents"] = documents
    if payload:
        attributes[SpanAttributes.INPUT_VALUE] = json.dumps(payload, ensure_ascii=False)
        attributes[SpanAttributes.INPUT_MIME_TYPE] = JSON_MIME_TYPE
    return attributes


def _usage(result: Any) -> Dict[str, Any]:
    """Voyage reports one total; every embedding or rerank token is input.

    Written on the promoted keys the collector reads (gen_ai.usage.*), once,
    on this span only. Never estimated.
    """
    total = getattr(result, "total_tokens", None)
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return {}
    return {
        SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS: total,
        SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS: total,
    }


def _response_attributes(
    operation: Operation, result: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> Dict[str, Any]:
    """Counts, dimension and usage from the returned object. Never vectors."""
    attributes = _usage(result)
    if operation is EMBED:
        embeddings = getattr(result, "embeddings", None)
        if isinstance(embeddings, (list, tuple)) and embeddings:
            first = embeddings[0]
            if hasattr(first, "__len__") and not isinstance(first, (str, bytes)):
                dimension = len(first)
                if _argument(operation, args, kwargs, "output_dtype") in _PACKED_DTYPES:
                    dimension *= 8
                attributes[EMBEDDING_DIMENSION] = dimension
                attributes[SpanAttributes.GEN_AI_EMBEDDINGS_DIMENSION_COUNT] = dimension
    else:
        results = getattr(result, "results", None)
        if isinstance(results, (list, tuple)):
            attributes[RERANK_RESULT_COUNT] = len(results)
    return attributes


def _output_content(operation: Operation, result: Any, config: TraceConfig) -> Dict[str, Any]:
    """Rerank scores in the client's order, by default, unless outputs are hidden.

    Scores are numbers, not input text, so hiding inputs keeps them (PRD J2).
    """
    if operation is not RERANK or config.hide_outputs:
        return {}
    results = getattr(result, "results", None)
    if not isinstance(results, (list, tuple)):
        return {}
    # At most MAX_CAPTURED_ITEMS, like texts and documents; result_count
    # stays exact.
    scores = [
        {
            "index": getattr(item, "index", None),
            "relevance_score": getattr(item, "relevance_score", None),
        }
        for item in results[:MAX_CAPTURED_ITEMS]
    ]
    return {
        SpanAttributes.OUTPUT_VALUE: json.dumps(scores),
        SpanAttributes.OUTPUT_MIME_TYPE: JSON_MIME_TYPE,
    }


def _set_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        try:
            span.set_attribute(key, value)
        except Exception:
            logger.debug("Could not set %s on the Voyage span", key, exc_info=True)


def _describe(error: BaseException, secrets: Sequence[str]) -> Tuple[str, str]:
    try:
        message = str(error)
    except Exception:
        message = "<unprintable {0}>".format(type(error).__name__)
    return type(error).__name__, _redact(message, secrets)


def _redact_pii(value: str, config: TraceConfig) -> str:
    """TraceConfig's PII redaction for text FiSpan does not mask.

    FiSpan masks attributes only; the status description and the exception
    event go through set_status / add_event unmasked, so with
    ``pii_redaction`` on they are redacted here. Callers remove the key first,
    so a key that looks like PII is still removed whole.
    """
    if not config.pii_redaction:
        return value
    if redact_pii_in_value is not None:
        redacted = redact_pii_in_value(value)
    else:  # pragma: no cover - same function, reached through TraceConfig
        redacted = config.mask("exception.message", value)
    return redacted if isinstance(redacted, str) else value


def _record_error(
    span: Span, error: BaseException, secrets: Sequence[str], config: TraceConfig
) -> None:
    name, message = _describe(error, secrets)
    try:
        stacktrace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    except Exception:
        stacktrace = ""
    message = _redact_pii(message, config)
    stacktrace = _redact_pii(_redact(stacktrace, secrets), config)
    # The SDK's record_exception would copy str(error) verbatim; the key may
    # be in it (a server echoing it back), so the event is built here.
    span.add_event(
        "exception",
        {
            "exception.type": "{0}.{1}".format(type(error).__module__, type(error).__qualname__),
            "exception.message": message,
            "exception.stacktrace": stacktrace,
        },
    )
    span.set_status(Status(StatusCode.ERROR, "{0}: {1}".format(name, message)))


def _end(span: Span) -> None:
    try:
        span.end()
    except Exception:
        logger.debug("Could not end the Voyage span", exc_info=True)


class _Call:
    """The tracing state for one wrapped call. Every step swallows its own errors."""

    __slots__ = ("_wrapper", "_args", "_kwargs", "_span", "_secrets", "_token")

    def __init__(
        self, wrapper: "_BaseWrapper", instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> None:
        self._wrapper = wrapper
        self._args = args
        self._kwargs = kwargs
        self._span: Optional[Span] = None
        self._secrets: List[str] = []
        self._token: Any = None
        operation = wrapper.operation
        attributes = _base_attributes(operation)
        try:
            self._secrets = _secrets(instance)
        except Exception:
            logger.debug("Could not read the Voyage client's key", exc_info=True)
        try:
            attributes.update(_request_attributes(operation, instance, args, kwargs))
        except Exception:
            logger.debug("Could not read Voyage request attributes", exc_info=True)
        try:
            attributes.update(
                _input_content(
                    operation,
                    args,
                    kwargs,
                    self._secrets,
                    wrapper.config,
                    wrapper.capture_content,
                )
            )
        except Exception:
            logger.debug("Could not capture Voyage request content", exc_info=True)
        try:
            self._span = wrapper.tracer.start_span(operation.span_name, attributes=attributes)
        except Exception:
            logger.debug("Could not start the Voyage span", exc_info=True)
            self._span = None

    def enter(self) -> None:
        """Make the span current so HTTP spans started by the SDK nest under it."""
        if self._span is None:
            return
        try:
            self._token = context_api.attach(trace_api.set_span_in_context(self._span))
        except Exception:
            logger.debug("Could not make the Voyage span current", exc_info=True)
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
            operation = self._wrapper.operation
            try:
                _set_attributes(
                    span, _response_attributes(operation, result, self._args, self._kwargs)
                )
            except Exception:
                logger.debug("Could not read Voyage response attributes", exc_info=True)
            try:
                _set_attributes(span, _output_content(operation, result, self._wrapper.config))
            except Exception:
                logger.debug("Could not capture Voyage response content", exc_info=True)
            span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("Could not finish the Voyage span", exc_info=True)
        finally:
            _end(span)

    def error(self, error: BaseException) -> None:
        span = self._span
        if span is None:
            return
        try:
            _record_error(span, error, self._secrets, self._wrapper.config)
        except Exception:
            logger.debug("Could not record the Voyage error", exc_info=True)
        finally:
            _end(span)

    def cancelled(self) -> None:
        # Cancellation is not an exception: no event, no result attributes.
        span = self._span
        if span is None:
            return
        try:
            span.set_attribute(CANCELLED, True)
            span.set_status(Status(StatusCode.ERROR, "cancelled"))
        except Exception:
            logger.debug("Could not mark the Voyage span cancelled", exc_info=True)
        finally:
            _end(span)


class _BaseWrapper:
    def __init__(
        self, tracer: Tracer, operation: Operation, config: TraceConfig, capture_content: bool
    ) -> None:
        self.tracer = tracer
        self.operation = operation
        self.config = config
        self.capture_content = capture_content

    def _begin(
        self, instance: Any, args: Sequence[Any], kwargs: Mapping[str, Any]
    ) -> Optional[_Call]:
        try:
            return _Call(self, instance, args, kwargs)
        except Exception:
            logger.debug("Could not trace the Voyage call", exc_info=True)
            return None


class SyncWrapper(_BaseWrapper):
    """Trace ``Client.embed`` / ``Client.rerank``."""

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


class AsyncWrapper(_BaseWrapper):
    """Trace ``AsyncClient.embed`` / ``AsyncClient.rerank``."""

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
