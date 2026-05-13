"""Milvus method wrappers for OpenTelemetry instrumentation."""

import json
import logging
from typing import Any, Callable

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

# FI canonical span-kind / IO keys. Optional dependency — gracefully degrade
# if fi-instrumentation isn't installed.
try:
    from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes

    _FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
    _FI_INPUT_VALUE = SpanAttributes.INPUT_VALUE
    _FI_INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
    _FI_OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
    _FI_OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
    _FI_RETRIEVER = FiSpanKindValues.RETRIEVER.value
except Exception:  # pragma: no cover
    _FI_SPAN_KIND = "gen_ai.span.kind"
    _FI_INPUT_VALUE = "input.value"
    _FI_INPUT_MIME_TYPE = "input.mime_type"
    _FI_OUTPUT_VALUE = "output.value"
    _FI_OUTPUT_MIME_TYPE = "output.mime_type"
    _FI_RETRIEVER = "RETRIEVER"

logger = logging.getLogger(__name__)


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)


def _summarize_milvus_search_input(kwargs: dict) -> str:
    """Render a JSON summary of a milvus search request for input.value."""
    data = kwargs.get("data")
    summary = {
        "limit": kwargs.get("limit", 10),
        "filter": str(kwargs["filter"]) if "filter" in kwargs else None,
        "anns_field": kwargs.get("anns_field"),
    }
    if isinstance(data, list):
        summary["vector_count"] = len(data)
        if data and isinstance(data[0], (list, tuple)):
            summary["vector_dim"] = len(data[0])
    return safe_json_dumps({k: v for k, v in summary.items() if v is not None})


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_attributes(self, operation: str, collection_name: str) -> dict:
        return {
            "db.system": "milvus",
            "db.operation.name": operation,
            "db.namespace": collection_name,
            "db.vector.collection.name": collection_name,
        }


class SearchWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("search", collection_name)
        attributes["db.vector.query.top_k"] = limit

        if "filter" in kwargs:
            attributes["db.vector.query.filter"] = str(kwargs["filter"])
        if "output_fields" in kwargs:
            attributes["db.vector.query.output_fields"] = safe_json_dumps(kwargs["output_fields"])

        # FI canonical retriever attributes — populate Type/Input/Output panels.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        attributes[_FI_INPUT_VALUE] = _summarize_milvus_search_input(kwargs)
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span("milvus search", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    total_results = sum(len(r) for r in result) if isinstance(result, list) else 0
                    span.set_attribute("db.vector.results.count", total_results)
                    span.set_attribute(_FI_OUTPUT_VALUE, safe_json_dumps(result))
                    span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class QueryWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("query", collection_name)
        attributes["db.vector.query.top_k"] = limit

        filter_expr = kwargs.get("filter")
        if filter_expr is not None:
            attributes["db.vector.query.filter"] = str(filter_expr)

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        attributes[_FI_INPUT_VALUE] = safe_json_dumps(
            {
                "filter": str(filter_expr) if filter_expr is not None else None,
                "limit": limit,
            }
        )
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span("milvus query", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    span.set_attribute("db.vector.results.count", len(result))
                    span.set_attribute(_FI_OUTPUT_VALUE, safe_json_dumps(result))
                    span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class InsertWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        data = kwargs.get("data") or (args[1] if len(args) > 1 else [])

        attributes = self._get_attributes("insert", collection_name)
        attributes["db.vector.upsert.count"] = len(data) if isinstance(data, list) else 1

        with self._tracer.start_as_current_span("milvus insert", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpsertWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        data = kwargs.get("data") or (args[1] if len(args) > 1 else [])

        attributes = self._get_attributes("upsert", collection_name)
        attributes["db.vector.upsert.count"] = len(data) if isinstance(data, list) else 1

        with self._tracer.start_as_current_span("milvus upsert", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        ids = kwargs.get("ids") or kwargs.get("pks")

        attributes = self._get_attributes("delete", collection_name)
        if ids:
            attributes["db.vector.delete.count"] = len(ids) if isinstance(ids, list) else 1

        with self._tracer.start_as_current_span("milvus delete", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class GetWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        ids = kwargs.get("ids") or (args[1] if len(args) > 1 else [])

        attributes = self._get_attributes("get", collection_name)
        attributes["db.vector.query.ids_count"] = len(ids) if isinstance(ids, list) else 1

        with self._tracer.start_as_current_span("milvus get", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    span.set_attribute("db.vector.results.count", len(result))
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class CreateCollectionWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")
        dimension = kwargs.get("dimension")

        attributes = self._get_attributes("create_collection", collection_name)
        if dimension:
            attributes["db.vector.dimension"] = dimension

        with self._tracer.start_as_current_span("milvus create_collection", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DropCollectionWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = kwargs.get("collection_name") or (args[0] if args else "unknown")

        attributes = self._get_attributes("drop_collection", collection_name)

        with self._tracer.start_as_current_span("milvus drop_collection", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
