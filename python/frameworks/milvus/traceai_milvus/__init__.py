"""
TraceAI Milvus Instrumentation

Provides OpenTelemetry instrumentation for Milvus vector database operations.
"""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_milvus.version import __version__
from traceai_milvus.package import _instruments
from traceai_milvus._wrappers import (
    SearchWrapper,
    QueryWrapper,
    InsertWrapper,
    UpsertWrapper,
    DeleteWrapper,
    GetWrapper,
    CreateCollectionWrapper,
    DropCollectionWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class MilvusInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Milvus vector database."""

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals = {}

        try:
            # MilvusClient API (recommended for new apps)
            from pymilvus import MilvusClient

            self._originals["client_search"] = MilvusClient.search
            self._originals["client_query"] = MilvusClient.query
            self._originals["client_insert"] = MilvusClient.insert
            self._originals["client_upsert"] = MilvusClient.upsert
            self._originals["client_delete"] = MilvusClient.delete
            self._originals["client_get"] = MilvusClient.get
            self._originals["client_create_collection"] = MilvusClient.create_collection
            self._originals["client_drop_collection"] = MilvusClient.drop_collection

            wrap_function_wrapper("pymilvus", "MilvusClient.search", SearchWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.query", QueryWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.insert", InsertWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.upsert", UpsertWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.delete", DeleteWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.get", GetWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.create_collection", CreateCollectionWrapper(tracer))
            wrap_function_wrapper("pymilvus", "MilvusClient.drop_collection", DropCollectionWrapper(tracer))

            logger.debug("Milvus instrumentation enabled")
        except ImportError as e:
            logger.warning(f"Could not instrument Milvus: {e}")

    def _uninstrument(self, **kwargs: Any) -> None:
        pass


__all__ = ["MilvusInstrumentor", "__version__"]
