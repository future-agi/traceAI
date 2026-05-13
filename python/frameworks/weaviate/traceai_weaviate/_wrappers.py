"""Weaviate method wrappers for OpenTelemetry instrumentation."""

import json
import logging
from typing import Any, Callable, Optional

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

# FI canonical span-kind / IO keys. Optional dependency.
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


def _weaviate_objects_summary(objects: Any) -> Optional[str]:
    """Render an (uuid, properties) summary of weaviate result objects."""
    try:
        out = []
        for obj in objects[:50]:
            entry: dict = {}
            uuid = getattr(obj, "uuid", None)
            if uuid is not None:
                entry["uuid"] = str(uuid)
            props = getattr(obj, "properties", None)
            if props is not None:
                entry["properties"] = props
            metadata = getattr(obj, "metadata", None)
            if metadata is not None and hasattr(metadata, "score"):
                entry["score"] = getattr(metadata, "score", None)
            if entry:
                out.append(entry)
        return safe_json_dumps(out) if out else None
    except Exception:
        return None


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_collection_name(self, instance: Any) -> str:
        try:
            if hasattr(instance, "_name"):
                return instance._name
            if hasattr(instance, "name"):
                return instance.name
        except Exception:
            pass
        return "unknown"

    def _get_attributes(self, operation: str, collection_name: str, query_type: str = None) -> dict:
        attrs = {
            "db.system": "weaviate",
            "db.operation.name": operation,
            "db.namespace": collection_name,
            "db.vector.collection.name": collection_name,
        }
        if query_type:
            attrs["db.vector.query.type"] = query_type
        return attrs


class NearVectorWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        limit = kwargs.get("limit", 10)
        certainty = kwargs.get("certainty")
        distance = kwargs.get("distance")

        attributes = self._get_attributes("query", collection_name, "near_vector")
        attributes["db.vector.query.top_k"] = limit
        if certainty:
            attributes["db.vector.query.certainty"] = certainty
        if distance:
            attributes["db.vector.query.distance"] = distance

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        near_vector = kwargs.get("near_vector", args[0] if args else None)
        input_summary = {
            "limit": limit,
            "certainty": certainty,
            "distance": distance,
        }
        if isinstance(near_vector, list):
            input_summary["vector_dim"] = len(near_vector)
        attributes[_FI_INPUT_VALUE] = safe_json_dumps(
            {k: v for k, v in input_summary.items() if v is not None}
        )
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span("weaviate near_vector", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "objects"):
                    span.set_attribute("db.vector.results.count", len(result.objects))
                    summary = _weaviate_objects_summary(result.objects)
                    if summary:
                        span.set_attribute(_FI_OUTPUT_VALUE, summary)
                        span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class NearTextWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        query = kwargs.get("query", args[0] if args else "")
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("query", collection_name, "near_text")
        attributes["db.vector.query.top_k"] = limit

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        if query:
            attributes[_FI_INPUT_VALUE] = (
                query if isinstance(query, str) else safe_json_dumps(query)
            )
            attributes[_FI_INPUT_MIME_TYPE] = (
                "text/plain" if isinstance(query, str) else "application/json"
            )

        with self._tracer.start_as_current_span("weaviate near_text", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "objects"):
                    span.set_attribute("db.vector.results.count", len(result.objects))
                    summary = _weaviate_objects_summary(result.objects)
                    if summary:
                        span.set_attribute(_FI_OUTPUT_VALUE, summary)
                        span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class HybridWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        query = kwargs.get("query", args[0] if args else "")
        alpha = kwargs.get("alpha", 0.5)
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("query", collection_name, "hybrid")
        attributes["db.vector.query.top_k"] = limit
        attributes["db.vector.query.hybrid.alpha"] = alpha

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        if query:
            attributes[_FI_INPUT_VALUE] = (
                query if isinstance(query, str) else safe_json_dumps(query)
            )
            attributes[_FI_INPUT_MIME_TYPE] = (
                "text/plain" if isinstance(query, str) else "application/json"
            )

        with self._tracer.start_as_current_span("weaviate hybrid", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "objects"):
                    span.set_attribute("db.vector.results.count", len(result.objects))
                    summary = _weaviate_objects_summary(result.objects)
                    if summary:
                        span.set_attribute(_FI_OUTPUT_VALUE, summary)
                        span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class Bm25Wrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        query = kwargs.get("query", args[0] if args else "")
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("query", collection_name, "bm25")
        attributes["db.vector.query.top_k"] = limit

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        if query:
            attributes[_FI_INPUT_VALUE] = (
                query if isinstance(query, str) else safe_json_dumps(query)
            )
            attributes[_FI_INPUT_MIME_TYPE] = (
                "text/plain" if isinstance(query, str) else "application/json"
            )

        with self._tracer.start_as_current_span("weaviate bm25", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "objects"):
                    span.set_attribute("db.vector.results.count", len(result.objects))
                    summary = _weaviate_objects_summary(result.objects)
                    if summary:
                        span.set_attribute(_FI_OUTPUT_VALUE, summary)
                        span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class FetchObjectsWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("fetch_objects", collection_name)
        attributes["db.vector.query.top_k"] = limit

        with self._tracer.start_as_current_span("weaviate fetch_objects", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "objects"):
                    span.set_attribute("db.vector.results.count", len(result.objects))
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class InsertWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)

        attributes = self._get_attributes("insert", collection_name)
        attributes["db.vector.upsert.count"] = 1

        with self._tracer.start_as_current_span("weaviate insert", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class InsertManyWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        objects = args[0] if args else kwargs.get("objects", [])

        attributes = self._get_attributes("insert_many", collection_name)
        attributes["db.vector.upsert.count"] = len(objects) if objects else 0

        with self._tracer.start_as_current_span("weaviate insert_many", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteByIdWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)

        attributes = self._get_attributes("delete_by_id", collection_name)
        attributes["db.vector.delete.count"] = 1

        with self._tracer.start_as_current_span("weaviate delete_by_id", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteManyWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)

        attributes = self._get_attributes("delete_many", collection_name)

        with self._tracer.start_as_current_span("weaviate delete_many", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "successful"):
                    span.set_attribute("db.vector.delete.count", result.successful)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
