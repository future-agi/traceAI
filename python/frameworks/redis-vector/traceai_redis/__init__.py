"""
TraceAI Redis Instrumentation

Provides OpenTelemetry instrumentation for Redis Vector Search operations.
"""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_redis.version import __version__
from traceai_redis.package import _instruments
from traceai_redis._wrappers import (
    SearchIndexSearchWrapper,
    SearchIndexLoadWrapper,
    SearchIndexQueryWrapper,
    FTSearchWrapper,
    FTCreateWrapper,
    FTDropIndexWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class RedisInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Redis Vector Search."""

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals = {}

        # Try to instrument RedisVL
        try:
            from redisvl.index import SearchIndex

            self._originals["redisvl_search"] = SearchIndex.search
            self._originals["redisvl_load"] = SearchIndex.load
            self._originals["redisvl_query"] = SearchIndex.query

            wrap_function_wrapper("redisvl.index", "SearchIndex.search", SearchIndexSearchWrapper(tracer))
            wrap_function_wrapper("redisvl.index", "SearchIndex.load", SearchIndexLoadWrapper(tracer))
            wrap_function_wrapper("redisvl.index", "SearchIndex.query", SearchIndexQueryWrapper(tracer))

            logger.debug("RedisVL instrumentation enabled")
        except ImportError:
            logger.debug("RedisVL not available, skipping instrumentation")

        # Try to instrument redis-py search commands
        try:
            from redis.commands.search import Search

            self._originals["ft_search"] = Search.search
            self._originals["ft_create"] = Search.create_index
            self._originals["ft_dropindex"] = Search.dropindex

            wrap_function_wrapper("redis.commands.search", "Search.search", FTSearchWrapper(tracer))
            wrap_function_wrapper("redis.commands.search", "Search.create_index", FTCreateWrapper(tracer))
            wrap_function_wrapper("redis.commands.search", "Search.dropindex", FTDropIndexWrapper(tracer))

            logger.debug("Redis Search instrumentation enabled")
        except ImportError as e:
            logger.warning(f"Could not instrument Redis Search: {e}")

    def _uninstrument(self, **kwargs: Any) -> None:
        pass


__all__ = ["RedisInstrumentor", "__version__"]
