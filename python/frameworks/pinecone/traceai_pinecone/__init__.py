"""OpenTelemetry instrumentation for Pinecone vector operations.

Use ``PineconeInstrumentor().instrument(tracer_provider=provider)`` before
creating sync REST, async REST or gRPC indexes. See the README for coverage.
"""

import inspect
import logging
from importlib import import_module
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_pinecone.version import __version__
from traceai_pinecone.package import _instruments
from traceai_pinecone._wrappers import (
    AsyncWrapper,
    QueryWrapper,
    UpsertWrapper,
    DeleteWrapper,
    FetchWrapper,
    UpdateWrapper,
    DescribeIndexStatsWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_OPERATIONS = {
    "query": QueryWrapper,
    "upsert": UpsertWrapper,
    "delete": DeleteWrapper,
    "fetch": FetchWrapper,
    "update": UpdateWrapper,
    "describe_index_stats": DescribeIndexStatsWrapper,
}


def _index_classes():
    # Concrete classes, not Pinecone.Index / Pinecone.IndexAsyncio factories.
    # SDK 7 moved Index; SDK 10 renamed the asyncio and gRPC classes.
    seen = set()
    for module_name, names in (
        ("pinecone", ("Index", "AsyncIndex")),
        ("pinecone.db_data", ("Index", "IndexAsyncio")),
        ("pinecone.grpc", ("GRPCIndex", "GrpcIndex")),
    ):
        try:
            module = import_module(module_name)
        except ImportError:
            # Older versions and installations without the optional extras
            # must keep working; importing this instrumentor requires no extras.
            continue
        for name in names:
            cls = getattr(module, name, None)
            if isinstance(cls, type) and cls not in seen:
                seen.add(cls)
                yield cls


class PineconeInstrumentor(BaseInstrumentor):
    """Trace query, upsert, delete, fetch, update and describe_index_stats.

    Supports sync REST, async REST and synchronous gRPC index methods exposed
    by the installed Pinecone SDK. Optional SDK extras remain optional.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer = trace_api.get_tracer(
            __name__, __version__, kwargs.get("tracer_provider")
        )
        self._originals = []
        try:
            for cls in _index_classes():
                for name, wrapper_class in _OPERATIONS.items():
                    original = getattr(cls, name, None)
                    if not callable(original):
                        continue
                    wrapper = wrapper_class(tracer=tracer)
                    if inspect.iscoroutinefunction(inspect.unwrap(original)):
                        wrapper = AsyncWrapper(wrapper)
                    wrap_function_wrapper(cls, name, wrapper)
                    self._originals.append((cls, name, original))
        except Exception:
            # Never leave a partially installed instrumentor behind.
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in reversed(self._originals):
            setattr(cls, name, original)
        self._originals.clear()


__all__ = ["PineconeInstrumentor", "__version__"]
