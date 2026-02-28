"""
TraceAI MongoDB Instrumentation

Provides OpenTelemetry instrumentation for MongoDB Atlas Vector Search operations.
"""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_mongodb.version import __version__
from traceai_mongodb.package import _instruments
from traceai_mongodb._wrappers import (
    AggregateWrapper,
    InsertOneWrapper,
    InsertManyWrapper,
    UpdateOneWrapper,
    UpdateManyWrapper,
    DeleteOneWrapper,
    DeleteManyWrapper,
    FindWrapper,
    FindOneWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class MongoDBInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for MongoDB Atlas Vector Search."""

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals = {}

        try:
            from pymongo.collection import Collection as MongoCollection

            self._originals["aggregate"] = MongoCollection.aggregate
            self._originals["insert_one"] = MongoCollection.insert_one
            self._originals["insert_many"] = MongoCollection.insert_many
            self._originals["update_one"] = MongoCollection.update_one
            self._originals["update_many"] = MongoCollection.update_many
            self._originals["delete_one"] = MongoCollection.delete_one
            self._originals["delete_many"] = MongoCollection.delete_many
            self._originals["find"] = MongoCollection.find
            self._originals["find_one"] = MongoCollection.find_one

            wrap_function_wrapper("pymongo.collection", "Collection.aggregate", AggregateWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.insert_one", InsertOneWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.insert_many", InsertManyWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.update_one", UpdateOneWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.update_many", UpdateManyWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.delete_one", DeleteOneWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.delete_many", DeleteManyWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.find", FindWrapper(tracer))
            wrap_function_wrapper("pymongo.collection", "Collection.find_one", FindOneWrapper(tracer))

            logger.debug("MongoDB instrumentation enabled")
        except ImportError as e:
            logger.warning(f"Could not instrument MongoDB: {e}")

    def _uninstrument(self, **kwargs: Any) -> None:
        pass


__all__ = ["MongoDBInstrumentor", "__version__"]
