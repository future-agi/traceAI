"""Milvus method wrappers for OpenTelemetry instrumentation."""

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

        with self._tracer.start_as_current_span("milvus search", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    total_results = sum(len(r) for r in result) if isinstance(result, list) else 0
                    span.set_attribute("db.vector.results.count", total_results)
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

        if "filter" in kwargs:
            attributes["db.vector.query.filter"] = str(kwargs["filter"])

        with self._tracer.start_as_current_span("milvus query", kind=SpanKind.CLIENT, attributes=attributes) as span:
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
