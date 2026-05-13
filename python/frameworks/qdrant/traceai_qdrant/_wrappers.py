"""Qdrant method wrappers for OpenTelemetry instrumentation."""

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


def _qdrant_points_summary(points: Any) -> Optional[str]:
    """Render an (id, score, payload) summary of a list of qdrant points."""
    try:
        out = []
        for p in points[:50]:
            entry = {"id": getattr(p, "id", None), "score": getattr(p, "score", None)}
            payload = getattr(p, "payload", None)
            if payload:
                entry["payload"] = payload
            out.append(entry)
        return safe_json_dumps(out) if out else None
    except Exception:
        return None


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_attributes(self, operation: str, collection_name: str) -> dict:
        return {
            "db.system": "qdrant",
            "db.operation.name": operation,
            "db.namespace": collection_name,
            "db.vector.collection.name": collection_name,
        }


class QueryPointsWrapper(BaseWrapper):
    """Wrapper for query_points (new API) and query methods."""

    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")
        limit = kwargs.get("limit", 10)
        with_payload = kwargs.get("with_payload", True)
        with_vectors = kwargs.get("with_vectors", False)
        score_threshold = kwargs.get("score_threshold")
        query_filter = kwargs.get("query_filter")

        attributes = self._get_attributes("query", collection_name)
        attributes.update({
            "db.vector.query.top_k": limit,
            "db.vector.query.with_payload": with_payload,
            "db.vector.query.with_vectors": with_vectors,
        })
        if score_threshold:
            attributes["db.vector.query.score_threshold"] = score_threshold
        if query_filter:
            attributes["db.vector.query.filter"] = safe_json_dumps(query_filter)

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        query = kwargs.get("query")
        input_summary = {
            "limit": limit,
            "filter": query_filter,
            "score_threshold": score_threshold,
        }
        if isinstance(query, list):
            input_summary["vector_dim"] = len(query)
        elif query is not None:
            input_summary["query"] = str(query)[:200]
        attributes[_FI_INPUT_VALUE] = safe_json_dumps(
            {k: v for k, v in input_summary.items() if v is not None}
        )
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span("qdrant query", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                # Handle both QueryResponse (query_points) and list results
                points = None
                if result:
                    if hasattr(result, 'points'):
                        points = result.points
                        span.set_attribute("db.vector.results.count", len(points))
                        scores = [p.score for p in points[:10] if hasattr(p, "score")]
                    else:
                        points = result
                        span.set_attribute("db.vector.results.count", len(result))
                        scores = [p.score for p in result[:10] if hasattr(p, "score")]
                    if scores:
                        span.set_attribute("db.vector.results.scores", safe_json_dumps(scores))
                    summary = _qdrant_points_summary(points) if points else None
                    if summary:
                        span.set_attribute(_FI_OUTPUT_VALUE, summary)
                        span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


# Keep SearchWrapper as alias for backwards compatibility
SearchWrapper = QueryPointsWrapper


class UpsertWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")
        points = kwargs.get("points", [])

        attributes = self._get_attributes("upsert", collection_name)
        attributes["db.vector.upsert.count"] = len(points) if points else 0

        with self._tracer.start_as_current_span("qdrant upsert", kind=SpanKind.CLIENT, attributes=attributes) as span:
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
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")
        points_selector = kwargs.get("points_selector")

        attributes = self._get_attributes("delete", collection_name)
        if points_selector:
            attributes["db.vector.delete.has_selector"] = True

        with self._tracer.start_as_current_span("qdrant delete", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class RetrieveWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")
        ids = kwargs.get("ids", [])

        attributes = self._get_attributes("retrieve", collection_name)
        attributes["db.vector.retrieve.ids_count"] = len(ids) if ids else 0

        with self._tracer.start_as_current_span("qdrant retrieve", kind=SpanKind.CLIENT, attributes=attributes) as span:
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


class ScrollWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("scroll", collection_name)
        attributes["db.vector.scroll.limit"] = limit

        with self._tracer.start_as_current_span("qdrant scroll", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and len(result) >= 2:
                    points, next_offset = result
                    span.set_attribute("db.vector.results.count", len(points))
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class RecommendWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")
        positive = kwargs.get("positive", [])
        negative = kwargs.get("negative", [])
        limit = kwargs.get("limit", 10)

        attributes = self._get_attributes("recommend", collection_name)
        attributes.update({
            "db.vector.recommend.positive_count": len(positive),
            "db.vector.recommend.negative_count": len(negative),
            "db.vector.query.top_k": limit,
        })

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        attributes[_FI_INPUT_VALUE] = safe_json_dumps(
            {
                "positive_count": len(positive),
                "negative_count": len(negative),
                "limit": limit,
            }
        )
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span("qdrant recommend", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    span.set_attribute("db.vector.results.count", len(result))
                    summary = _qdrant_points_summary(result)
                    if summary:
                        span.set_attribute(_FI_OUTPUT_VALUE, summary)
                        span.set_attribute(_FI_OUTPUT_MIME_TYPE, "application/json")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class CountWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = args[0] if args else kwargs.get("collection_name", "unknown")

        attributes = self._get_attributes("count", collection_name)

        with self._tracer.start_as_current_span("qdrant count", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "count"):
                    span.set_attribute("db.vector.collection.count", result.count)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
