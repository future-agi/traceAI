"""Trace the ``elasticsearch`` Python client's search, kNN and write calls.

Covers ``search`` (including ``knn`` and hybrid requests), ``knn_search`` where the client still has it, and the
document writes ``index``, ``bulk`` and ``delete``, for both ``Elasticsearch`` and ``AsyncElasticsearch``. Only
the outermost client call is traced. Embedding vectors are never recorded, and neither are raw request or
response bodies: the spans carry an explicit set of fields, nothing is dumped whole.
"""
from __future__ import annotations

import contextvars
import datetime
import decimal
import inspect
import json
import numbers
import uuid
from collections.abc import Collection
from importlib import import_module
from itertools import islice
from typing import Any

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues
from fi_instrumentation.fi_types import SpanAttributes as A
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
from wrapt import wrap_function_wrapper

from .version import __version__

PROVIDER = "elasticsearch"
_LIMIT = 32768
_DOCUMENT_LIMIT = 4096
_MAX_IDS = 100
_MAX_DOCUMENTS = 20
_CLIENTS = ("Elasticsearch", "AsyncElasticsearch")
_TEXTUAL = (datetime.date, datetime.datetime, datetime.time, decimal.Decimal, uuid.UUID)

# method -> span kind
_METHODS = {
    "search": FiSpanKindValues.RETRIEVER,
    "knn_search": FiSpanKindValues.RETRIEVER,
    "index": FiSpanKindValues.VECTOR_DB,
    "bulk": FiSpanKindValues.VECTOR_DB,
    "delete": FiSpanKindValues.VECTOR_DB,
}
# Plain request parameters worth recording; content-bearing ones are handled separately.
_PARAMETERS = ("size", "from_", "min_score", "search_type", "track_total_hits", "routing", "preference",
               "terminate_after", "timeout", "refresh", "pipeline")

# True while a traced client call runs, so a nested public call does not create a duplicate span.
_INSIDE: contextvars.ContextVar[bool] = contextvars.ContextVar("traceai_elasticsearch_inside", default=False)


def _is_number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _plain(value: Any) -> Any:
    """Array-likes (numpy arrays and scalars, torch tensors) as plain Python values."""
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and not isinstance(value, (str, bytes, dict, list, tuple)):
        try:
            return tolist()
        except Exception:  # noqa: BLE001 - an exotic array type is reduced to a type note below
            return value
    return value


def _scrub(value: Any, depth: int = 0) -> Any:
    """Content without embedding vectors: numeric sequences of three or more values and ``query_vector``
    fields are dropped. Values of unknown types are reduced to a type note, never to their text."""
    value = _plain(value)
    if isinstance(value, (str, bool)) or value is None or _is_number(value):
        return value
    if isinstance(value, _TEXTUAL):
        return str(value)
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k): _scrub(v, depth + 1) for k, v in value.items() if k != "query_vector"}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if len(items) >= 3 and all(_is_number(_plain(v)) for v in items):
            return "[vector omitted]"
        return [_scrub(v, depth + 1) for v in items]
    return f"[{type(value).__name__}]"


def _dump(value: Any, limit: int) -> str:
    return json.dumps(_scrub(value), default=lambda v: f"[{type(v).__name__}]", sort_keys=True)[:limit]


def _simple(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or (
        isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value))


# Top-level keys of a search request body, merged into the arguments when the deprecated ``body=`` form is used.
_SEARCH_BODY_KEYS = {"query": "query", "knn": "knn", "rank": "rank", "rescore": "rescore", "post_filter": "post_filter",
                     "retriever": "retriever", "size": "size", "from": "from_", "min_score": "min_score",
                     "track_total_hits": "track_total_hits", "terminate_after": "terminate_after"}


def _arguments(wrapped: Any, method: str, args: tuple, kwargs: dict) -> dict:
    try:
        bound = inspect.signature(wrapped).bind_partial(*args, **kwargs)
        arguments = dict(bound.arguments)
    except (TypeError, ValueError):
        arguments = dict(kwargs)
    body = arguments.get("body")
    if method in ("search", "knn_search") and isinstance(body, dict):
        for key, name in _SEARCH_BODY_KEYS.items():
            if key in body:
                arguments.setdefault(name, body[key])
    return arguments


def _capture_input(config: TraceConfig) -> bool:
    return not (config.hide_inputs or config.hide_input_text or config.hide_input_messages or config.hide_input_images)


def _capture_output(config: TraceConfig) -> bool:
    return not (config.hide_outputs or config.hide_output_text or config.hide_output_messages)


def _index_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):
        return ",".join(value)
    return None


def _bulk_lines(operations: Any) -> list:
    if isinstance(operations, (bytes, str)):
        text = operations.decode() if isinstance(operations, bytes) else operations
        lines = []
        for line in text.splitlines():
            if line.strip():
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    return []
        return lines
    return list(operations) if isinstance(operations, (list, tuple)) else []


def _bulk_documents(operations: Any) -> tuple[int, list]:
    """The write count and the document lines of a bulk request (action lines are not content)."""
    count, documents = 0, []
    expecting_document = False
    for line in _bulk_lines(operations):
        if expecting_document:
            documents.append(line)
            expecting_document = False
            continue
        if isinstance(line, dict) and len(line) == 1:
            action = next(iter(line))
            if action in ("index", "create", "update"):
                count += 1
                expecting_document = True
    return count, documents


def _retriever_k(retriever: Any, depth: int = 0) -> Any:
    """The k of the first kNN retriever inside a (possibly nested) retriever request."""
    if depth > 4 or not isinstance(retriever, dict):
        return None
    knn = retriever.get("knn")
    if isinstance(knn, dict) and _is_number(knn.get("k")):
        return knn["k"]
    for value in retriever.values():
        found = _retriever_k(value, depth + 1) if isinstance(value, dict) else None
        if found is None and isinstance(value, list):
            found = next((k for k in (_retriever_k(v, depth + 1) for v in value) if k is not None), None)
        if found is not None:
            return found
    return None


def _start(tracer: Any, config: TraceConfig, method: str, wrapped: Any, args: tuple, kwargs: dict):
    arguments = _arguments(wrapped, method, args, kwargs)
    index = _index_name(arguments.get("index"))
    attrs: dict[str, Any] = {
        A.GEN_AI_PROVIDER_NAME: PROVIDER,
        "db.system": PROVIDER,
        "db.operation.name": method,
    }
    if method in ("search", "knn_search"):
        attrs[A.GEN_AI_OPERATION_NAME] = "retrieval"
    if index:
        attrs["db.namespace"] = index
        attrs["db.vector.index.name"] = index
    capture = _capture_input(config)
    params = {n: arguments[n] for n in _PARAMETERS if arguments.get(n) is not None and _simple(arguments[n])}
    if method in ("search", "knn_search"):
        knn = arguments.get("knn")
        knn_requests = [k for k in (knn if isinstance(knn, (list, tuple)) else [knn]) if isinstance(k, dict)]
        k_value = knn_requests[0].get("k") if knn_requests else _retriever_k(arguments.get("retriever"))
        top_k = k_value if _is_number(k_value) else arguments.get("size")
        if _is_number(top_k):
            attrs["db.vector.query.top_k"] = top_k
            attrs[A.GEN_AI_RETRIEVAL_TOP_K] = top_k
        if knn_requests:
            first = knn_requests[0]
            if isinstance(first.get("field"), str):
                attrs["elasticsearch.field"] = first["field"]
            if _is_number(first.get("num_candidates")):
                attrs["elasticsearch.num_candidates"] = first["num_candidates"]
            vector = first.get("query_vector")
            if isinstance(vector, (list, tuple)) and all(_is_number(v) for v in vector):
                attrs["elasticsearch.query_vector_dimensions"] = len(vector)
        if capture:
            request = {k: arguments[k] for k in ("query", "knn", "retriever", "rank", "rescore", "post_filter") if arguments.get(k)}
            if request:
                attrs[A.GEN_AI_RETRIEVAL_QUERY] = _dump(request, _LIMIT)
    elif method == "bulk":
        count, documents = _bulk_documents(arguments.get("operations") or arguments.get("body"))
        attrs["db.vector.upsert.count"] = count
        if capture and documents:
            attrs[A.INPUT_VALUE] = _dump(list(islice(documents, _MAX_DOCUMENTS)), _LIMIT)
            attrs[A.INPUT_MIME_TYPE] = "application/json"
    elif method == "index":
        document = arguments.get("document") if arguments.get("document") is not None else arguments.get("body")
        if isinstance(arguments.get("id"), str):
            attrs["elasticsearch.document_id"] = arguments["id"]
        if capture and isinstance(document, dict):
            attrs[A.INPUT_VALUE] = _dump(document, _LIMIT)
            attrs[A.INPUT_MIME_TYPE] = "application/json"
    elif method == "delete" and isinstance(arguments.get("id"), str):
        attrs["elasticsearch.document_id"] = arguments["id"]
    if params:
        attrs[A.GEN_AI_REQUEST_PARAMETERS] = json.dumps(params, default=str, sort_keys=True)
    return tracer.start_span(f"elasticsearch.{method}", kind=SpanKind.CLIENT, attributes=attrs,
                             fi_span_kind=_METHODS[method])


def _body(result: Any) -> dict:
    body = getattr(result, "body", result)
    return body if isinstance(body, dict) else {}


def _record(span: Any, config: TraceConfig, method: str, result: Any) -> None:
    body = _body(result)
    if method in ("search", "knn_search"):
        hits_block = body.get("hits") if isinstance(body.get("hits"), dict) else {}
        hits = [h for h in (hits_block.get("hits") or []) if isinstance(h, dict)]
        span.set_attribute("db.vector.results.count", len(hits))
        total = hits_block.get("total")
        total_value = total.get("value") if isinstance(total, dict) else total
        if _is_number(total_value):
            span.set_attribute("elasticsearch.total_hits", total_value)
        if _is_number(body.get("took")):
            span.set_attribute("elasticsearch.took_ms", body["took"])
        if _capture_output(config):
            # Ids and scores identify and rank the result content, so the output settings cover them too.
            ids = [str(h["_id"]) for h in hits if h.get("_id") is not None][:_MAX_IDS]
            if ids:
                span.set_attribute("db.vector.results.ids", ids)
            scores = [h["_score"] for h in hits if _is_number(h.get("_score"))][:_MAX_IDS]
            if scores:
                span.set_attribute("db.vector.results.scores", [float(s) for s in scores])
                span.set_attribute("elasticsearch.top_score", float(scores[0]))
        if _capture_output(config):
            for position, hit in enumerate(hits[:_MAX_DOCUMENTS]):
                prefix = f"{A.RETRIEVAL_DOCUMENTS}.{position}.document"
                if hit.get("_id") is not None:
                    span.set_attribute(f"{prefix}.id", str(hit["_id"]))
                content = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit.get("fields")
                if isinstance(content, dict):
                    span.set_attribute(f"{prefix}.content", _dump(content, _DOCUMENT_LIMIT))
                if _is_number(hit.get("_score")):
                    span.set_attribute(f"{prefix}.score", float(hit["_score"]))
    elif method == "bulk":
        items = [i for i in (body.get("items") or []) if isinstance(i, dict)]
        errors = bool(body.get("errors"))
        span.set_attribute("elasticsearch.has_errors", errors)
        span.set_attribute("elasticsearch.indexed_count", len(items))
        if _is_number(body.get("took")):
            span.set_attribute("elasticsearch.took_ms", body["took"])
        failed = sum(1 for i in items for r in i.values()
                     if isinstance(r, dict) and _is_number(r.get("status")) and r["status"] >= 400)
        span.set_attribute("elasticsearch.failed_count", failed)
        if errors:
            span.set_status(Status(StatusCode.ERROR))
    else:
        if isinstance(body.get("result"), str):
            span.set_attribute("elasticsearch.result", body["result"])
        if _is_number(body.get("_version")):
            span.set_attribute("elasticsearch.version", body["_version"])


def _failure(span: Any, exc: BaseException) -> None:
    # Server error text can echo request content or credentials: record only the type and status code.
    span.set_attribute("error.type", type(exc).__name__)
    status_code = getattr(exc, "status_code", None)
    if _is_number(status_code):
        span.set_attribute("elasticsearch.status_code", status_code)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


def _begin(tracer: Any, config: TraceConfig, method: str, wrapped: Any, args: tuple, kwargs: dict):
    try:
        return _start(tracer, config, method, wrapped, args, kwargs)
    except Exception:  # never let tracing break the host call
        return None


def _finish(span: Any, config: TraceConfig, method: str, result: Any) -> None:
    try:
        _record(span, config, method, result)
    except Exception:  # never let tracing break the caller's result
        pass
    finally:
        span.end()


def _wrap(tracer: Any, config: TraceConfig, method: str, asynchronous: bool):
    if asynchronous:
        async def async_wrapper(wrapped, instance, args, kwargs):
            if _INSIDE.get():
                return await wrapped(*args, **kwargs)
            span = _begin(tracer, config, method, wrapped, args, kwargs)
            if span is None:
                return await wrapped(*args, **kwargs)
            token = _INSIDE.set(True)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = await wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            finally:
                _INSIDE.reset(token)
            _finish(span, config, method, result)
            return result

        return async_wrapper

    def wrapper(wrapped, instance, args, kwargs):
        if _INSIDE.get():
            return wrapped(*args, **kwargs)
        span = _begin(tracer, config, method, wrapped, args, kwargs)
        if span is None:
            return wrapped(*args, **kwargs)
        token = _INSIDE.set(True)
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                result = wrapped(*args, **kwargs)
        except BaseException as exc:
            _failure(span, exc)
            raise
        finally:
            _INSIDE.reset(token)
        _finish(span, config, method, result)
        return result

    return wrapper


class ElasticsearchInstrumentor(BaseInstrumentor):
    """Trace ``Elasticsearch`` and ``AsyncElasticsearch`` search, kNN and document write calls."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("elasticsearch >= 8, < 10",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        self._originals = []
        module = import_module("elasticsearch")
        for name in _CLIENTS:
            cls = getattr(module, name, None)
            if cls is None:
                continue
            for method in _METHODS:
                original = cls.__dict__.get(method)
                if original is None:  # this client release does not have the method
                    continue
                wrap_function_wrapper("elasticsearch", f"{name}.{method}",
                                      _wrap(tracer, config, method, asynchronous=name.startswith("Async")))
                self._originals.append((cls, method, original))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in getattr(self, "_originals", []):
            setattr(cls, name, original)
        self._originals = []


__all__ = ["ElasticsearchInstrumentor", "__version__"]
