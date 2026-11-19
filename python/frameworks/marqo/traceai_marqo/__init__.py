"""Trace the ``marqo`` Python SDK's index operations.

Covers ``Index.search``, ``add_documents``, ``get_documents`` and ``get_document``. The SDK is
synchronous and has no streaming calls, so there are no async or stream variants. Only the
outermost SDK call is traced. Do not also instrument ``requests`` for the same calls.
"""
from __future__ import annotations

import contextvars
import inspect
import json
from importlib import import_module
from typing import Any, Collection, Optional

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes as A
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
from wrapt import wrap_function_wrapper

from .version import __version__

PROVIDER = "marqo"
_LIMIT = 32768
_DOCUMENT_LIMIT = 4096
_MAX_IDS = 100
_MAX_DOCUMENTS = 20
_MODULE = "marqo.index"

# method -> (span kind, parameters worth recording; content-bearing ones are handled separately)
_METHODS = {
    "search": (FiSpanKindValues.RETRIEVER, (
        "limit", "offset", "search_method", "searchable_attributes", "attributes_to_retrieve", "device",
        "approximate", "ef_search", "rerank_depth", "track_total_hits", "approximate_threshold",
        "language", "show_highlights")),
    "add_documents": (FiSpanKindValues.CHAIN, (
        "client_batch_size", "device", "tensor_fields", "use_existing_tensors")),
    "get_documents": (FiSpanKindValues.RETRIEVER, ("expose_facets",)),
    "get_document": (FiSpanKindValues.RETRIEVER, ("expose_facets",)),
}

# True while a traced SDK call runs, so a nested public call does not create a duplicate span.
_INSIDE: contextvars.ContextVar[bool] = contextvars.ContextVar("traceai_marqo_inside", default=False)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Document content without embedding vectors: numeric arrays and ``vector`` fields are dropped."""
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k): _scrub(v, depth + 1) for k, v in value.items() if k != "vector"}
    if isinstance(value, (list, tuple)):
        if len(value) >= 3 and all(_is_number(v) for v in value):
            return "[vector omitted]"
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dump(value: Any, limit: int) -> str:
    return json.dumps(_scrub(value), default=str, sort_keys=True)[:limit]


def _simple(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or (
        isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value))


def _arguments(wrapped: Any, args: tuple, kwargs: dict) -> dict:
    try:
        bound = inspect.signature(wrapped).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _capture_input(config: TraceConfig) -> bool:
    return not (config.hide_inputs or config.hide_input_text or config.hide_input_messages or config.hide_input_images)


def _capture_output(config: TraceConfig) -> bool:
    return not (config.hide_outputs or config.hide_output_text or config.hide_output_messages)


def _start(tracer: Any, config: TraceConfig, instance: Any, method: str, wrapped: Any, args: tuple, kwargs: dict):
    kind, names = _METHODS[method]
    arguments = _arguments(wrapped, args, kwargs)
    index_name = getattr(instance, "index_name", None)
    attrs: dict[str, Any] = {
        A.GEN_AI_PROVIDER_NAME: PROVIDER,
        "db.system": PROVIDER,
        "db.operation.name": method,
    }
    if method != "add_documents":
        attrs[A.GEN_AI_OPERATION_NAME] = "retrieval"
    if isinstance(index_name, str):
        attrs["db.namespace"] = index_name
        attrs["db.vector.index.name"] = index_name
    capture = _capture_input(config)
    params = {n: arguments[n] for n in names if arguments.get(n) is not None and _simple(arguments[n])}
    if method == "search":
        if capture and isinstance(arguments.get("filter_string"), str):
            params["filter_string"] = arguments["filter_string"]
        if _is_number(params.get("limit")):
            attrs["db.vector.query.top_k"] = params["limit"]
            attrs[A.GEN_AI_RETRIEVAL_TOP_K] = params["limit"]
        query = arguments.get("q")
        if capture and isinstance(query, str):
            attrs[A.GEN_AI_RETRIEVAL_QUERY] = query[:_LIMIT]
        elif capture and isinstance(query, dict):
            attrs[A.GEN_AI_RETRIEVAL_QUERY] = _dump(query, _LIMIT)
    elif method == "add_documents":
        documents = arguments.get("documents")
        if isinstance(documents, (list, tuple)):
            attrs["db.vector.upsert.count"] = len(documents)
            if capture:
                attrs[A.INPUT_VALUE] = _dump(list(documents), _LIMIT)
                attrs[A.INPUT_MIME_TYPE] = "application/json"
    if params:
        attrs[A.GEN_AI_REQUEST_PARAMETERS] = json.dumps(params, default=str, sort_keys=True)
    return tracer.start_span(f"marqo.{method}", kind=SpanKind.CLIENT, attributes=attrs, fi_span_kind=kind)


def _hits(method: str, result: Any) -> list:
    if method == "search" and isinstance(result, dict):
        found = result.get("hits")
    elif method == "get_documents" and isinstance(result, dict):
        found = [r for r in (result.get("results") or []) if isinstance(r, dict) and r.get("_found")]
    elif method == "get_document" and isinstance(result, dict) and result.get("_id") is not None:
        found = [result]
    else:
        found = []
    return [h for h in (found or []) if isinstance(h, dict)]


def _record(span: Any, config: TraceConfig, method: str, result: Any) -> None:
    if method == "add_documents":
        batches = result if isinstance(result, (list, tuple)) else [result]
        batches = [b for b in batches if isinstance(b, dict)]
        span.set_attribute("marqo.errors", any(bool(b.get("errors")) for b in batches))
        failed = sum(
            1 for b in batches for item in b.get("items") or []
            if isinstance(item, dict) and _is_number(item.get("status")) and item["status"] >= 400)
        span.set_attribute("marqo.failed_count", failed)
        return
    hits = _hits(method, result)
    span.set_attribute("db.vector.results.count", len(hits))
    ids = [str(h["_id"]) for h in hits if h.get("_id") is not None][:_MAX_IDS]
    if ids:
        span.set_attribute("db.vector.results.ids", ids)
    scores = [h["_score"] for h in hits if _is_number(h.get("_score"))][:_MAX_IDS]
    if scores:
        span.set_attribute("db.vector.results.scores", [float(s) for s in scores])
    if _capture_output(config):
        for position, hit in enumerate(hits[:_MAX_DOCUMENTS]):
            prefix = f"{A.RETRIEVAL_DOCUMENTS}.{position}.document"
            if hit.get("_id") is not None:
                span.set_attribute(f"{prefix}.id", str(hit["_id"]))
            content = {k: v for k, v in hit.items() if not str(k).startswith("_")}
            span.set_attribute(f"{prefix}.content", _dump(content, _DOCUMENT_LIMIT))
            if _is_number(hit.get("_score")):
                span.set_attribute(f"{prefix}.score", float(hit["_score"]))


def _failure(span: Any, exc: BaseException) -> None:
    # Server error text can echo request content or credentials: record only the type.
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


def _wrap(tracer: Any, config: TraceConfig, method: str):
    def wrapper(wrapped, instance, args, kwargs):
        if _INSIDE.get():
            return wrapped(*args, **kwargs)
        span = _start(tracer, config, instance, method, wrapped, args, kwargs)
        token = _INSIDE.set(True)
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                result = wrapped(*args, **kwargs)
        except BaseException as exc:
            _failure(span, exc)
            raise
        finally:
            _INSIDE.reset(token)
        try:
            _record(span, config, method, result)
        except Exception:  # never let tracing break the caller's result
            pass
        finally:
            span.end()
        return result

    return wrapper


class MarqoInstrumentor(BaseInstrumentor):
    """Trace ``marqo.index.Index`` search, add_documents, get_documents and get_document."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("marqo >= 3.5.0, < 4",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        self._originals = []
        cls = import_module(_MODULE).Index
        for method in _METHODS:
            original = cls.__dict__.get(method)
            if original is None:  # this SDK version does not have the method
                continue
            wrap_function_wrapper(_MODULE, f"Index.{method}", _wrap(tracer, config, method))
            self._originals.append((cls, method, original))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in getattr(self, "_originals", []):
            setattr(cls, name, original)
        self._originals = []


__all__ = ["MarqoInstrumentor", "__version__"]
