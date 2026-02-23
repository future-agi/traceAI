"""
TraceAI Qdrant Instrumentation

Provides OpenTelemetry instrumentation for Qdrant vector database operations.
"""

import logging
from importlib import import_module
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_qdrant.version import __version__
from traceai_qdrant.package import _instruments
from traceai_qdrant._wrappers import (
    QueryPointsWrapper,
    UpsertWrapper,
    DeleteWrapper,
    RetrieveWrapper,
    ScrollWrapper,
    CountWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "qdrant_client"


class QdrantInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Qdrant vector database."""

    __slots__ = (
        "_original_query_points",
        "_original_query",
        "_original_upsert",
        "_original_delete",
        "_original_retrieve",
        "_original_scroll",
        "_original_count",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        try:
            from qdrant_client import QdrantClient
        except ImportError:
            logger.warning("Could not import QdrantClient")
            return

        self._original_query_points = QdrantClient.query_points
        self._original_query = QdrantClient.query
        self._original_upsert = QdrantClient.upsert
        self._original_delete = QdrantClient.delete
        self._original_retrieve = QdrantClient.retrieve
        self._original_scroll = QdrantClient.scroll
        self._original_count = QdrantClient.count

        wrap_function_wrapper(_MODULE, "QdrantClient.query_points", QueryPointsWrapper(tracer=tracer))
        wrap_function_wrapper(_MODULE, "QdrantClient.query", QueryPointsWrapper(tracer=tracer))
        wrap_function_wrapper(_MODULE, "QdrantClient.upsert", UpsertWrapper(tracer=tracer))
        wrap_function_wrapper(_MODULE, "QdrantClient.delete", DeleteWrapper(tracer=tracer))
        wrap_function_wrapper(_MODULE, "QdrantClient.retrieve", RetrieveWrapper(tracer=tracer))
        wrap_function_wrapper(_MODULE, "QdrantClient.scroll", ScrollWrapper(tracer=tracer))
        wrap_function_wrapper(_MODULE, "QdrantClient.count", CountWrapper(tracer=tracer))

        logger.debug("Qdrant instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        from qdrant_client import QdrantClient

        QdrantClient.query_points = self._original_query_points
        QdrantClient.query = self._original_query
        QdrantClient.upsert = self._original_upsert
        QdrantClient.delete = self._original_delete
        QdrantClient.retrieve = self._original_retrieve
        QdrantClient.scroll = self._original_scroll
        QdrantClient.count = self._original_count


__all__ = ["QdrantInstrumentor", "__version__"]
