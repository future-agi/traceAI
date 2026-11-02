"""OpenTelemetry instrumentation for Weaviate collection queries and writes."""

from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from traceai_weaviate._wrappers import OperationWrapper
from traceai_weaviate.package import _instruments
from traceai_weaviate.version import __version__

_QUERY_CLASSES = {
    "near_vector": "_NearVectorQuery",
    "near_text": "_NearTextQuery",
    "hybrid": "_HybridQuery",
    "bm25": "_BM25Query",
    "fetch_objects": "_FetchObjectsQuery",
}
_DATA_METHODS = ("insert", "insert_many", "delete_by_id", "delete_many")
_MISSING = object()


def _module(name):
    try:
        return import_module(name)
    except ModuleNotFoundError as exc:
        # Only an absent SDK target is optional, not a missing dependency.
        if exc.name and (name == exc.name or name.startswith(exc.name + ".")):
            return None
        raise


def _targets():
    """Resolve the pre-async, syncify, and executor-based v4 SDK layouts."""
    targets = []
    for method, class_name in _QUERY_CLASSES.items():
        base = f"weaviate.collections.queries.{method}.query"
        sync_module = _module(base + ".sync") or import_module(base)
        targets.append((getattr(sync_module, class_name), method, False))
        async_module = _module(base + ".async_") or sync_module
        async_class = getattr(async_module, class_name + "Async", None)
        if async_class is not None:
            targets.append((async_class, method, True))
    data = "weaviate.collections.data"
    sync_module = _module(data + ".sync") or import_module(data)
    async_module = _module(data + ".async_") or _module(data + ".data")
    for method in _DATA_METHODS:
        targets.append((sync_module._DataCollection, method, False))
        if async_module is not None:
            targets.append((async_module._DataCollectionAsync, method, True))
    return targets


class WeaviateInstrumentor(BaseInstrumentor):
    """Instrument supported sync/async v4 collection methods.

    ``config`` accepts a TraceConfig. ``capture_content=True`` opts in to query
    text and returned document properties; vectors and filters are never captured.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        capture_content = kwargs.get("capture_content", False)
        if not isinstance(capture_content, bool):
            raise TypeError("capture_content must be a bool")
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, kwargs.get("tracer_provider")),
            config=config,
        )
        # Resolve everything before changing SDK methods, and roll back a partial
        # installation. Save local descriptors so inherited methods stay inherited.
        targets = _targets()
        self._originals = []
        try:
            for cls, method, is_async in targets:
                original = cls.__dict__.get(method, _MISSING)
                wrapper = OperationWrapper(
                    tracer, method, config=config, capture_content=capture_content
                )
                replacement = FunctionWrapper(
                    getattr(cls, method), wrapper.async_call if is_async else wrapper
                )
                setattr(cls, method, replacement)
                self._originals.append((cls, method, original, replacement))
        except Exception:
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, method, original, replacement in reversed(self._originals):
            # Do not overwrite instrumentation installed by someone else later.
            if cls.__dict__.get(method) is not replacement:
                continue
            if original is _MISSING:
                delattr(cls, method)
            else:
                setattr(cls, method, original)
        self._originals.clear()


__all__ = ["WeaviateInstrumentor", "__version__"]
