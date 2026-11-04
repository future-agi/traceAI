"""OpenTelemetry instrumentation for local, remote and async LanceDB operations."""

import importlib
import inspect
import logging
from collections.abc import Collection
from typing import Any

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from traceai_lancedb._wrappers import OperationWrapper, remember_parent
from traceai_lancedb.package import _instruments
from traceai_lancedb.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_QUERY_METHODS = (
    "to_list",
    "to_arrow",
    "to_batches",
    "to_pandas",
    "to_df",
    "to_pydantic",
    "to_polars",
)
_TARGETS = (
    ("lancedb.table", ("LanceTable", "AsyncTable"), ("add", "update", "delete")),
    (
        "lancedb.db",
        ("LanceDBConnection", "AsyncConnection"),
        ("create_table", "open_table", "drop_table"),
    ),
    ("lancedb.remote.table", ("RemoteTable",), ("add", "update", "delete")),
    (
        "lancedb.remote.db",
        ("RemoteDBConnection",),
        ("create_table", "open_table", "drop_table"),
    ),
)


class LanceDBInstrumentor(BaseInstrumentor):
    """Trace query materialization and database writes without capturing content."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _patch(self, cls: type, method: str, wrapper: Any) -> None:
        # Patch the defining class only: inherited implementations must not be
        # shadowed with persistent wrappers when instrumentation is disabled.
        original = vars(cls).get(method)
        if original is None:
            return
        replacement = FunctionWrapper(original, wrapper)
        setattr(cls, method, replacement)
        self._originals.append((cls, method, original, replacement))

    def _instrument(self, **kwargs: Any) -> None:
        tracer = trace_api.get_tracer(
            __name__, __version__, kwargs.get("tracer_provider")
        )
        self._originals = []
        try:
            for module_name, names, methods in _TARGETS:
                try:
                    module = importlib.import_module(module_name)
                except ImportError as error:
                    logger.debug(
                        "LanceDB surface unavailable: %s (%s)",
                        module_name,
                        type(error).__name__,
                    )
                    # Optional remote/async surfaces are absent on old versions.
                    continue
                for name in names:
                    cls = getattr(module, name, None)
                    if cls is None:
                        continue
                    for method in methods:
                        wrapper = OperationWrapper(tracer, method, method)
                        original = vars(cls).get(method)
                        self._patch(
                            cls,
                            method,
                            wrapper.asynchronous
                            if inspect.iscoroutinefunction(original)
                            else wrapper,
                        )

            query = importlib.import_module("lancedb.query")
            roots = tuple(
                getattr(query, name)
                for name in ("LanceQueryBuilder", "AsyncQueryBase", "BaseQueryBuilder")
                if hasattr(query, name)
            )
            for cls in vars(query).values():
                if (
                    not inspect.isclass(cls)
                    or cls.__module__ != query.__name__
                    or not issubclass(cls, roots)
                ):
                    continue
                self._patch(cls, "__init__", remember_parent)
                for method in _QUERY_METHODS:
                    wrapper = OperationWrapper(tracer, "search", method)
                    original = vars(cls).get(method)
                    self._patch(
                        cls,
                        method,
                        wrapper.asynchronous
                        if inspect.iscoroutinefunction(original)
                        else wrapper,
                    )
        except Exception:
            # An unsupported API must not leave a half-instrumented process.
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, method, original, replacement in reversed(
            getattr(self, "_originals", [])
        ):
            if vars(cls).get(method) is replacement:
                setattr(cls, method, original)
        self._originals = []


__all__ = ["LanceDBInstrumentor", "__version__"]
