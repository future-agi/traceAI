"""
TraceAI ChromaDB Instrumentation

Provides OpenTelemetry instrumentation for ChromaDB vector database operations.

Usage:
    from fi_instrumentation import register
    from traceai_chromadb import ChromaDBInstrumentor

    trace_provider = register(project_name="my-rag-app")
    ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

    # Now all ChromaDB operations are automatically traced
    import chromadb
    client = chromadb.Client()
    collection = client.create_collection("my-collection")
    collection.add(ids=["id1"], documents=["Hello world"])
    results = collection.query(query_texts=["Hello"], n_results=5)
"""

import logging
from importlib import import_module
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_chromadb.version import __version__
from traceai_chromadb.package import _instruments
from traceai_chromadb._wrappers import (
    AddWrapper,
    QueryWrapper,
    GetWrapper,
    UpdateWrapper,
    UpsertWrapper,
    DeleteWrapper,
    CountWrapper,
    PeekWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "chromadb"


class ChromaDBInstrumentor(BaseInstrumentor):
    """
    OpenTelemetry instrumentor for ChromaDB vector database.

    Instruments the following Collection operations:
    - add: Add documents/embeddings to collection
    - query: Search for similar documents
    - get: Retrieve documents by ID
    - update: Update existing documents
    - upsert: Insert or update documents
    - delete: Delete documents
    - count: Get collection count
    - peek: Preview collection contents
    """

    __slots__ = (
        "_original_add",
        "_original_query",
        "_original_get",
        "_original_update",
        "_original_upsert",
        "_original_delete",
        "_original_count",
        "_original_peek",
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

        chromadb = import_module(_MODULE)

        # Get the Collection class
        try:
            from chromadb.api.models.Collection import Collection
        except ImportError:
            try:
                Collection = chromadb.Collection
            except AttributeError:
                logger.warning("Could not find ChromaDB Collection class")
                return

        # Store original methods
        self._original_add = Collection.add
        self._original_query = Collection.query
        self._original_get = Collection.get
        self._original_update = Collection.update
        self._original_upsert = Collection.upsert
        self._original_delete = Collection.delete
        self._original_count = Collection.count
        self._original_peek = Collection.peek

        # Wrap Collection methods
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.add",
            wrapper=AddWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.query",
            wrapper=QueryWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.get",
            wrapper=GetWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.update",
            wrapper=UpdateWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.upsert",
            wrapper=UpsertWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.delete",
            wrapper=DeleteWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.count",
            wrapper=CountWrapper(tracer=tracer),
        )
        wrap_function_wrapper(
            module="chromadb.api.models.Collection",
            name="Collection.peek",
            wrapper=PeekWrapper(tracer=tracer),
        )

        logger.debug("ChromaDB instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        try:
            from chromadb.api.models.Collection import Collection
        except ImportError:
            chromadb = import_module(_MODULE)
            Collection = chromadb.Collection

        # Restore original methods
        Collection.add = self._original_add
        Collection.query = self._original_query
        Collection.get = self._original_get
        Collection.update = self._original_update
        Collection.upsert = self._original_upsert
        Collection.delete = self._original_delete
        Collection.count = self._original_count
        Collection.peek = self._original_peek

        logger.debug("ChromaDB instrumentation disabled")


__all__ = ["ChromaDBInstrumentor", "__version__"]
