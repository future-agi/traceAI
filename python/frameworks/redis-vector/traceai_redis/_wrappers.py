"""Redis method wrappers for OpenTelemetry instrumentation."""

import json
import logging
from typing import Any, Callable

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_index_name(self, instance: Any) -> str:
        try:
            if hasattr(instance, "name"):
                return instance.name
            if hasattr(instance, "index_name"):
                return instance.index_name
            if hasattr(instance, "_name"):
                return instance._name
        except Exception:
            pass
        return "unknown"

    def _get_attributes(self, operation: str, index_name: str) -> dict:
        return {
            "db.system": "redis",
            "db.operation.name": operation,
            "db.namespace": index_name,
            "db.vector.collection.name": index_name,
        }


# RedisVL wrappers
class SearchIndexSearchWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        index_name = self._get_index_name(instance)

        attributes = self._get_attributes("search", index_name)
        attributes["db.vector.query.type"] = "vector_search"

        # Try to extract query info
        query = args[0] if args else kwargs.get("query")
        if query:
            if hasattr(query, "num_results"):
                attributes["db.vector.query.top_k"] = query.num_results
            if hasattr(query, "vector_field_name"):
                attributes["db.vector.query.field"] = query.vector_field_name
            if hasattr(query, "return_fields"):
                attributes["db.vector.query.return_fields"] = safe_json_dumps(query.return_fields)

        with self._tracer.start_as_current_span("redis vector_search", kind=SpanKind.CLIENT, attributes=attributes) as span:
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


class SearchIndexLoadWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        index_name = self._get_index_name(instance)
        data = args[0] if args else kwargs.get("data", [])

        attributes = self._get_attributes("load", index_name)
        if hasattr(data, "__len__"):
            attributes["db.vector.upsert.count"] = len(data)

        with self._tracer.start_as_current_span("redis load", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class SearchIndexQueryWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        index_name = self._get_index_name(instance)

        attributes = self._get_attributes("query", index_name)

        with self._tracer.start_as_current_span("redis query", kind=SpanKind.CLIENT, attributes=attributes) as span:
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


# redis-py FT.* command wrappers
class FTSearchWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        index_name = self._get_index_name(instance)
        query = args[0] if args else kwargs.get("query")

        attributes = self._get_attributes("ft_search", index_name)

        # Check if it's a vector query (contains KNN)
        query_str = str(query) if query else ""
        if "KNN" in query_str.upper():
            attributes["db.vector.query.type"] = "knn"

        with self._tracer.start_as_current_span("redis ft_search", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result and hasattr(result, "total"):
                    span.set_attribute("db.vector.results.count", result.total)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class FTCreateWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        index_name = self._get_index_name(instance)

        attributes = self._get_attributes("ft_create", index_name)

        with self._tracer.start_as_current_span("redis ft_create", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class FTDropIndexWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        index_name = self._get_index_name(instance)

        attributes = self._get_attributes("ft_dropindex", index_name)

        with self._tracer.start_as_current_span("redis ft_dropindex", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
