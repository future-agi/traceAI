"""OpenTelemetry tracing for ChromaDB's sync and async collection operations.

Use ``ChromaDBInstrumentor().instrument(tracer_provider=provider)`` before
calling collection methods. See the README for the supported operations.
"""

import inspect
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
    SearchWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_WRAPPERS = {
    "add": AddWrapper,
    "query": QueryWrapper,
    "get": GetWrapper,
    "update": UpdateWrapper,
    "upsert": UpsertWrapper,
    "delete": DeleteWrapper,
    "count": CountWrapper,
    "peek": PeekWrapper,
    "search": SearchWrapper,
}


class ChromaDBInstrumentor(BaseInstrumentor):
    """Trace Collection and, when available, AsyncCollection data operations."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer = trace_api.get_tracer(
            __name__, __version__, kwargs.get("tracer_provider")
        )
        self._originals = []
        try:
            for class_name in ("Collection", "AsyncCollection"):
                module_name = f"chromadb.api.models.{class_name}"
                try:
                    collection_class = getattr(import_module(module_name), class_name)
                except ModuleNotFoundError as exc:
                    # AsyncCollection is absent at the supported Chroma 0.4.0 floor.
                    if class_name == "AsyncCollection" and exc.name == module_name:
                        continue
                    raise
                for method_name, wrapper_type in _WRAPPERS.items():
                    original = getattr(collection_class, method_name, None)
                    if original is None:
                        continue
                    signature = inspect.signature(original)
                    # wrapt passes the bound method's arguments separately from self.
                    signature = signature.replace(
                        parameters=list(signature.parameters.values())[1:]
                    )
                    wrapper = wrapper_type(tracer, signature)
                    callback = (
                        wrapper.async_call
                        if inspect.iscoroutinefunction(original)
                        else wrapper
                    )
                    self._originals.append((collection_class, method_name, original))
                    # Positional arguments work with both wrapt 1.x and 2.x.
                    wrap_function_wrapper(collection_class, method_name, callback)
        except Exception:
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for collection_class, method_name, original in reversed(self._originals):
            setattr(collection_class, method_name, original)
        self._originals.clear()


__all__ = ["ChromaDBInstrumentor", "__version__"]
