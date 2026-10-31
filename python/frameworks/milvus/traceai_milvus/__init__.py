"""OpenTelemetry instrumentation for PyMilvus client operations."""

from collections.abc import Collection
from importlib import import_module
from typing import Any

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from traceai_milvus._wrappers import OperationWrapper
from traceai_milvus.package import _instruments
from traceai_milvus.version import __version__

_OPERATIONS = (
    "search",
    "query",
    "insert",
    "upsert",
    "delete",
    "get",
    "create_collection",
    "drop_collection",
    "hybrid_search",
    "search_iterator",
    "query_iterator",
)


class MilvusInstrumentor(BaseInstrumentor):
    """Trace available MilvusClient and AsyncMilvusClient methods.

    Pass ``config=TraceConfig(...)`` to control input/error privacy, or use the
    FI_HIDE_* environment variables. No connection to Milvus is made here.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, kwargs.get("tracer_provider")),
            config=config,
        )
        sdk = import_module("pymilvus")
        self._originals = []
        # Capture descriptors before patching either class, including inherited
        # methods. Optional APIs are absent at the declared PyMilvus floor.
        targets = []
        for class_name in ("MilvusClient", "AsyncMilvusClient"):
            cls = getattr(sdk, class_name, None)
            if cls is None:
                continue
            for name in _OPERATIONS:
                original = getattr(cls, name, None)
                if callable(original):
                    owned = name in vars(cls)
                    descriptor = vars(cls)[name] if owned else original
                    targets.append((cls, name, descriptor, owned))
        try:
            for cls, name, original, owned in targets:
                wrapper = OperationWrapper(tracer, config, name, original)
                installed = FunctionWrapper(original, wrapper)
                setattr(cls, name, installed)
                self._originals.append((cls, name, original, owned, installed, wrapper))
        except Exception:
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original, owned, installed, wrapper in reversed(self._originals):
            # A later instrumentor may retain this wrapper. Disable it without
            # discarding their patch, including iterator proxies already returned.
            wrapper.enabled = False
            if vars(cls).get(name) is installed:
                if owned:
                    setattr(cls, name, original)
                else:
                    delattr(cls, name)
        self._originals.clear()


__all__ = ["MilvusInstrumentor", "__version__"]
