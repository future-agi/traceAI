"""
TraceAI Qdrant Instrumentation

Provides OpenTelemetry instrumentation for Qdrant vector database operations.
"""

import logging
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

    __slots__ = ("_original_methods",)

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

        # Query APIs vary by SDK version (and the optional fastembed extra).
        # In particular query_points replaced search, and query is not always
        # present. Only patch methods the installed client actually exposes.
        self._original_methods = {}
        for name, wrapper in (
            ("query_points", QueryPointsWrapper),
            ("query", QueryPointsWrapper),
            ("search", QueryPointsWrapper),
            ("upsert", UpsertWrapper),
            ("delete", DeleteWrapper),
            ("retrieve", RetrieveWrapper),
            ("scroll", ScrollWrapper),
            ("count", CountWrapper),
        ):
            original = getattr(QdrantClient, name, None)
            if original is not None:
                self._original_methods[name] = original
                wrap_function_wrapper(_MODULE, f"QdrantClient.{name}", wrapper(tracer=tracer))

        logger.debug("Qdrant instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        from qdrant_client import QdrantClient

        for name, original in self._original_methods.items():
            setattr(QdrantClient, name, original)
        self._original_methods.clear()


__all__ = ["QdrantInstrumentor", "__version__"]
