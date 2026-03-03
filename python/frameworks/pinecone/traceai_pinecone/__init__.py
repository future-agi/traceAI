"""
TraceAI Pinecone Instrumentation

Provides OpenTelemetry instrumentation for Pinecone vector database operations.

Usage:
    from fi_instrumentation import register
    from traceai_pinecone import PineconeInstrumentor

    trace_provider = register(project_name="my-rag-app")
    PineconeInstrumentor().instrument(tracer_provider=trace_provider)

    # Now all Pinecone operations are automatically traced
    import pinecone
    pc = pinecone.Pinecone(api_key="...")
    index = pc.Index("my-index")
    results = index.query(vector=[0.1] * 1536, top_k=10)
"""

import logging
from importlib import import_module
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_pinecone.version import __version__
from traceai_pinecone.package import _instruments
from traceai_pinecone._wrappers import (
    QueryWrapper,
    UpsertWrapper,
    DeleteWrapper,
    FetchWrapper,
    UpdateWrapper,
    DescribeIndexStatsWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "pinecone"


class PineconeInstrumentor(BaseInstrumentor):
    """
    OpenTelemetry instrumentor for Pinecone vector database.

    Instruments the following operations:
    - query: Vector similarity search
    - upsert: Insert or update vectors
    - delete: Delete vectors
    - fetch: Retrieve vectors by ID
    - update: Update vector metadata
    - describe_index_stats: Get index statistics
    """

    __slots__ = (
        "_original_query",
        "_original_upsert",
        "_original_delete",
        "_original_fetch",
        "_original_update",
        "_original_describe_index_stats",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(
            __name__,
            __version__,
            tracer_provider,
        )

        pinecone = import_module(_MODULE)

        # Get the Index class - handle both v3 and v4 API
        try:
            # Pinecone v3+
            index_class = pinecone.Index
        except AttributeError:
            # Pinecone v4+ might have different structure
            try:
                from pinecone import Index
                index_class = Index
            except ImportError:
                logger.warning("Could not find Pinecone Index class")
                return

        # Store original methods
        self._original_query = index_class.query
        self._original_upsert = index_class.upsert
        self._original_delete = index_class.delete
        self._original_fetch = index_class.fetch
        self._original_update = index_class.update
        self._original_describe_index_stats = index_class.describe_index_stats

        # Wrap methods
        wrap_function_wrapper(
            module=_MODULE,
            name="Index.query",
            wrapper=QueryWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="Index.upsert",
            wrapper=UpsertWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="Index.delete",
            wrapper=DeleteWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="Index.fetch",
            wrapper=FetchWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="Index.update",
            wrapper=UpdateWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="Index.describe_index_stats",
            wrapper=DescribeIndexStatsWrapper(tracer=tracer),
        )

        logger.debug("Pinecone instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        pinecone = import_module(_MODULE)

        try:
            index_class = pinecone.Index
        except AttributeError:
            from pinecone import Index
            index_class = Index

        # Restore original methods
        index_class.query = self._original_query
        index_class.upsert = self._original_upsert
        index_class.delete = self._original_delete
        index_class.fetch = self._original_fetch
        index_class.update = self._original_update
        index_class.describe_index_stats = self._original_describe_index_stats

        logger.debug("Pinecone instrumentation disabled")


__all__ = ["PineconeInstrumentor", "__version__"]
