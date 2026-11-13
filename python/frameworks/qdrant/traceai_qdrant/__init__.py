"""
TraceAI Qdrant Instrumentation

Provides OpenTelemetry instrumentation for Qdrant vector database operations.
"""

import inspect
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
    RecommendWrapper,
    CountWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "qdrant_client"
_QUERY_METHODS = (
    "query_points", "query_batch_points", "query_points_groups",
    "query", "query_batch", "search", "search_batch", "search_groups",
)
_RECOMMEND_METHODS = ("recommend", "recommend_batch", "recommend_groups")


class QdrantInstrumentor(BaseInstrumentor):
    """Instrument available synchronous and asynchronous Qdrant client APIs."""

    __slots__ = ("_original_methods",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)
        self._original_methods = {}

        try:
            import qdrant_client
        except ImportError:
            logger.warning("Could not import qdrant_client")
            return

        # The dependency floor predates AsyncQdrantClient; newer SDKs removed
        # search/recommend and the optional text APIs. Never invent missing APIs.
        for class_name in ("QdrantClient", "AsyncQdrantClient"):
            client_class = getattr(qdrant_client, class_name, None)
            if client_class is None:
                continue
            for name, wrapper_type in (
                *((name, QueryPointsWrapper) for name in _QUERY_METHODS),
                *((name, RecommendWrapper) for name in _RECOMMEND_METHODS),
                ("upsert", UpsertWrapper),
                ("delete", DeleteWrapper),
                ("retrieve", RetrieveWrapper),
                ("scroll", ScrollWrapper),
                ("count", CountWrapper),
            ):
                original = getattr(client_class, name, None)
                if original is None:
                    continue
                self._original_methods[(client_class, name)] = original
                query_kwargs = {"method_name": name} if issubclass(wrapper_type, QueryPointsWrapper) else {}
                wrapper = wrapper_type(tracer=tracer, **query_kwargs)
                callback = wrapper.async_call if inspect.iscoroutinefunction(original) else wrapper
                wrap_function_wrapper(_MODULE, f"{class_name}.{name}", callback)
        logger.debug("Qdrant instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        for (client_class, name), original in self._original_methods.items():
            setattr(client_class, name, original)
        self._original_methods.clear()


__all__ = ["QdrantInstrumentor", "__version__"]
