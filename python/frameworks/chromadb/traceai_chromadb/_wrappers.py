"""
ChromaDB method wrappers for OpenTelemetry instrumentation.
"""

import json
import logging
from typing import Any, Callable, Optional, List

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from traceai_chromadb._attributes import (
    VectorDBAttributes as Attrs,
    get_common_attributes,
    safe_json_dumps,
)

logger = logging.getLogger(__name__)


class BaseWrapper:
    """Base wrapper class for ChromaDB operations."""

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_collection_name(self, instance: Any) -> str:
        """Extract collection name from ChromaDB Collection instance."""
        try:
            if hasattr(instance, "name"):
                return instance.name
            if hasattr(instance, "_name"):
                return instance._name
        except Exception:
            pass
        return "unknown"

    def _get_collection_metadata(self, instance: Any) -> Optional[dict]:
        """Extract collection metadata from ChromaDB Collection instance."""
        try:
            if hasattr(instance, "metadata"):
                return instance.metadata
        except Exception:
            pass
        return None


class AddWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.add() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        # Extract parameters
        ids = kwargs.get("ids", [])
        embeddings = kwargs.get("embeddings")
        documents = kwargs.get("documents")
        metadatas = kwargs.get("metadatas")

        # Count items being added
        item_count = len(ids) if ids else 0
        if not item_count and documents:
            item_count = len(documents)
        if not item_count and embeddings:
            item_count = len(embeddings)

        # Get dimensions if embeddings provided
        dimensions = None
        if embeddings and len(embeddings) > 0:
            first_embedding = embeddings[0]
            if isinstance(first_embedding, (list, tuple)):
                dimensions = len(first_embedding)

        span_name = "chroma add"

        attributes = get_common_attributes("chroma", "add", collection_name)
        attributes[Attrs.UPSERT_COUNT] = item_count

        if dimensions:
            attributes[Attrs.UPSERT_DIMENSIONS] = dimensions

        if documents:
            attributes["db.vector.documents.count"] = len(documents)

        if metadatas:
            attributes["db.vector.metadatas.count"] = len(metadatas)

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class QueryWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.query() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        # Extract query parameters
        n_results = kwargs.get("n_results", 10)
        where = kwargs.get("where")
        where_document = kwargs.get("where_document")
        include = kwargs.get("include", ["metadatas", "documents", "distances"])

        # Check query type
        query_embeddings = kwargs.get("query_embeddings")
        query_texts = kwargs.get("query_texts")

        span_name = "chroma query"

        attributes = get_common_attributes("chroma", "query", collection_name)
        attributes[Attrs.QUERY_TOP_K] = n_results

        if where:
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(where)

        if where_document:
            attributes["db.vector.query.where_document"] = safe_json_dumps(where_document)

        if include:
            attributes["db.vector.query.include"] = safe_json_dumps(include)

        if query_embeddings:
            attributes["db.vector.query.type"] = "embedding"
            attributes["db.vector.query.embedding_count"] = len(query_embeddings)
        elif query_texts:
            attributes["db.vector.query.type"] = "text"
            attributes["db.vector.query.text_count"] = len(query_texts)

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                # Extract results info
                if result:
                    if "ids" in result and result["ids"]:
                        # Result is a dict with lists
                        first_query_ids = result["ids"][0] if result["ids"] else []
                        span.set_attribute(Attrs.RESULTS_COUNT, len(first_query_ids))

                        if len(first_query_ids) > 0:
                            span.set_attribute(Attrs.RESULTS_IDS, safe_json_dumps(first_query_ids[:10]))

                    if "distances" in result and result["distances"]:
                        first_query_distances = result["distances"][0] if result["distances"] else []
                        if first_query_distances:
                            span.set_attribute(Attrs.RESULTS_SCORES, safe_json_dumps(first_query_distances[:10]))

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class GetWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.get() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        # Extract parameters
        ids = kwargs.get("ids")
        where = kwargs.get("where")
        where_document = kwargs.get("where_document")
        include = kwargs.get("include", ["metadatas", "documents"])
        limit = kwargs.get("limit")
        offset = kwargs.get("offset")

        span_name = "chroma get"

        attributes = get_common_attributes("chroma", "get", collection_name)

        if ids:
            attributes["db.vector.get.ids_count"] = len(ids)

        if where:
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(where)

        if where_document:
            attributes["db.vector.query.where_document"] = safe_json_dumps(where_document)

        if include:
            attributes["db.vector.query.include"] = safe_json_dumps(include)

        if limit:
            attributes["db.vector.get.limit"] = limit

        if offset:
            attributes["db.vector.get.offset"] = offset

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                # Extract results info
                if result and "ids" in result:
                    span.set_attribute(Attrs.RESULTS_COUNT, len(result["ids"]))

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpdateWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.update() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        # Extract parameters
        ids = kwargs.get("ids", [])
        embeddings = kwargs.get("embeddings")
        documents = kwargs.get("documents")
        metadatas = kwargs.get("metadatas")

        item_count = len(ids) if ids else 0

        span_name = "chroma update"

        attributes = get_common_attributes("chroma", "update", collection_name)
        attributes["db.vector.update.count"] = item_count

        if embeddings:
            attributes["db.vector.update.has_embeddings"] = True

        if documents:
            attributes["db.vector.update.has_documents"] = True

        if metadatas:
            attributes["db.vector.update.has_metadatas"] = True

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpsertWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.upsert() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        # Extract parameters
        ids = kwargs.get("ids", [])
        embeddings = kwargs.get("embeddings")
        documents = kwargs.get("documents")
        metadatas = kwargs.get("metadatas")

        item_count = len(ids) if ids else 0

        # Get dimensions if embeddings provided
        dimensions = None
        if embeddings and len(embeddings) > 0:
            first_embedding = embeddings[0]
            if isinstance(first_embedding, (list, tuple)):
                dimensions = len(first_embedding)

        span_name = "chroma upsert"

        attributes = get_common_attributes("chroma", "upsert", collection_name)
        attributes[Attrs.UPSERT_COUNT] = item_count

        if dimensions:
            attributes[Attrs.UPSERT_DIMENSIONS] = dimensions

        if documents:
            attributes["db.vector.documents.count"] = len(documents)

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.delete() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        # Extract parameters
        ids = kwargs.get("ids")
        where = kwargs.get("where")
        where_document = kwargs.get("where_document")

        span_name = "chroma delete"

        attributes = get_common_attributes("chroma", "delete", collection_name)

        if ids:
            attributes[Attrs.DELETE_COUNT] = len(ids)

        if where:
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(where)

        if where_document:
            attributes["db.vector.query.where_document"] = safe_json_dumps(where_document)

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class CountWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.count() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)

        span_name = "chroma count"

        attributes = get_common_attributes("chroma", "count", collection_name)

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                if isinstance(result, int):
                    span.set_attribute("db.vector.collection.count", result)

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class PeekWrapper(BaseWrapper):
    """Wrapper for ChromaDB Collection.peek() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        collection_name = self._get_collection_name(instance)
        limit = kwargs.get("limit", 10)

        span_name = "chroma peek"

        attributes = get_common_attributes("chroma", "peek", collection_name)
        attributes["db.vector.peek.limit"] = limit

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                if result and "ids" in result:
                    span.set_attribute(Attrs.RESULTS_COUNT, len(result["ids"]))

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
