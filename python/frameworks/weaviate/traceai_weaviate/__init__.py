"""
TraceAI Weaviate Instrumentation

Provides OpenTelemetry instrumentation for Weaviate vector database operations (v4 API).
"""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_weaviate.version import __version__
from traceai_weaviate.package import _instruments
from traceai_weaviate._wrappers import (
    NearVectorWrapper,
    NearTextWrapper,
    HybridWrapper,
    Bm25Wrapper,
    FetchObjectsWrapper,
    InsertWrapper,
    InsertManyWrapper,
    DeleteByIdWrapper,
    DeleteManyWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class WeaviateInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Weaviate vector database (v4 API)."""

    __slots__ = (
        "_originals",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals = {}

        try:
            # Weaviate v4 query methods (sync versions)
            from weaviate.collections.queries.near_vector.query.sync import _NearVectorQuery
            from weaviate.collections.queries.near_text.query.sync import _NearTextQuery
            from weaviate.collections.queries.hybrid.query.sync import _HybridQuery
            from weaviate.collections.queries.bm25.query.sync import _BM25Query
            from weaviate.collections.queries.fetch_objects.query.sync import _FetchObjectsQuery
            from weaviate.collections.data.sync import _DataCollection

            self._originals["near_vector"] = _NearVectorQuery.near_vector
            self._originals["near_text"] = _NearTextQuery.near_text
            self._originals["hybrid"] = _HybridQuery.hybrid
            self._originals["bm25"] = _BM25Query.bm25
            self._originals["fetch_objects"] = _FetchObjectsQuery.fetch_objects
            self._originals["insert"] = _DataCollection.insert
            self._originals["insert_many"] = _DataCollection.insert_many
            self._originals["delete_by_id"] = _DataCollection.delete_by_id
            self._originals["delete_many"] = _DataCollection.delete_many

            wrap_function_wrapper("weaviate.collections.queries.near_vector.query.sync", "_NearVectorQuery.near_vector", NearVectorWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.queries.near_text.query.sync", "_NearTextQuery.near_text", NearTextWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.queries.hybrid.query.sync", "_HybridQuery.hybrid", HybridWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.queries.bm25.query.sync", "_BM25Query.bm25", Bm25Wrapper(tracer))
            wrap_function_wrapper("weaviate.collections.queries.fetch_objects.query.sync", "_FetchObjectsQuery.fetch_objects", FetchObjectsWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.data.sync", "_DataCollection.insert", InsertWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.data.sync", "_DataCollection.insert_many", InsertManyWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.data.sync", "_DataCollection.delete_by_id", DeleteByIdWrapper(tracer))
            wrap_function_wrapper("weaviate.collections.data.sync", "_DataCollection.delete_many", DeleteManyWrapper(tracer))

            logger.debug("Weaviate instrumentation enabled")
        except ImportError as e:
            logger.warning(f"Could not instrument Weaviate: {e}")

    def _uninstrument(self, **kwargs: Any) -> None:
        # Restore originals
        pass


__all__ = ["WeaviateInstrumentor", "__version__"]
