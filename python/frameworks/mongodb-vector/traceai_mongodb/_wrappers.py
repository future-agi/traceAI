"""MongoDB method wrappers for OpenTelemetry instrumentation."""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)


def extract_vector_search_info(pipeline: List[Dict]) -> Optional[Dict[str, Any]]:
    """Extract $vectorSearch stage info from aggregation pipeline."""
    for stage in pipeline:
        if "$vectorSearch" in stage:
            vs = stage["$vectorSearch"]
            return {
                "index": vs.get("index", "unknown"),
                "path": vs.get("path"),
                "limit": vs.get("limit", 10),
                "num_candidates": vs.get("numCandidates"),
                "query_vector_dims": len(vs.get("queryVector", [])) if vs.get("queryVector") else None,
            }
    return None


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_collection_name(self, instance: Any) -> str:
        try:
            return instance.name
        except Exception:
            return "unknown"

    def _get_database_name(self, instance: Any) -> str:
        try:
            return instance.database.name
        except Exception:
            return "unknown"

    def _get_attributes(self, operation: str, collection_name: str, database: str = None) -> dict:
        attrs = {
            "db.system": "mongodb",
            "db.operation.name": operation,
            "db.namespace": collection_name,
            "db.vector.collection.name": collection_name,
        }
        if database:
            attrs["db.name"] = database
        return attrs


class AggregateWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)
        pipeline = args[0] if args else kwargs.get("pipeline", [])

        attributes = self._get_attributes("aggregate", collection_name, database)

        # Check for $vectorSearch stage
        vector_search_info = extract_vector_search_info(pipeline)
        if vector_search_info:
            attributes["db.vector.query.type"] = "vector_search"
            attributes["db.vector.index.name"] = vector_search_info["index"]
            if vector_search_info["limit"]:
                attributes["db.vector.query.top_k"] = vector_search_info["limit"]
            if vector_search_info["num_candidates"]:
                attributes["db.vector.query.num_candidates"] = vector_search_info["num_candidates"]
            if vector_search_info["query_vector_dims"]:
                attributes["db.vector.query.vector_dimensions"] = vector_search_info["query_vector_dims"]
            if vector_search_info["path"]:
                attributes["db.vector.query.field"] = vector_search_info["path"]

        attributes["db.mongodb.pipeline_stages"] = len(pipeline)

        with self._tracer.start_as_current_span("mongodb aggregate", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class InsertOneWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)

        attributes = self._get_attributes("insert_one", collection_name, database)
        attributes["db.vector.upsert.count"] = 1

        with self._tracer.start_as_current_span("mongodb insert_one", kind=SpanKind.CLIENT, attributes=attributes) as span:
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
        database = self._get_database_name(instance)
        documents = args[0] if args else kwargs.get("documents", [])

        attributes = self._get_attributes("insert_many", collection_name, database)
        attributes["db.vector.upsert.count"] = len(documents) if documents else 0

        with self._tracer.start_as_current_span("mongodb insert_many", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpdateOneWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)

        attributes = self._get_attributes("update_one", collection_name, database)

        with self._tracer.start_as_current_span("mongodb update_one", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpdateManyWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)

        attributes = self._get_attributes("update_many", collection_name, database)

        with self._tracer.start_as_current_span("mongodb update_many", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    span.set_attribute("db.vector.update.matched_count", result.matched_count)
                    span.set_attribute("db.vector.update.modified_count", result.modified_count)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteOneWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)

        attributes = self._get_attributes("delete_one", collection_name, database)
        attributes["db.vector.delete.count"] = 1

        with self._tracer.start_as_current_span("mongodb delete_one", kind=SpanKind.CLIENT, attributes=attributes) as span:
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
        database = self._get_database_name(instance)

        attributes = self._get_attributes("delete_many", collection_name, database)

        with self._tracer.start_as_current_span("mongodb delete_many", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result:
                    span.set_attribute("db.vector.delete.count", result.deleted_count)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class FindWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)
        limit = kwargs.get("limit", 0)

        attributes = self._get_attributes("find", collection_name, database)
        if limit:
            attributes["db.vector.query.top_k"] = limit

        with self._tracer.start_as_current_span("mongodb find", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class FindOneWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        collection_name = self._get_collection_name(instance)
        database = self._get_database_name(instance)

        attributes = self._get_attributes("find_one", collection_name, database)
        attributes["db.vector.query.top_k"] = 1

        with self._tracer.start_as_current_span("mongodb find_one", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_attribute("db.vector.results.count", 1 if result else 0)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
