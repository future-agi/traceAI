"""OpenTelemetry instrumentation for Redis vector search operations."""

import logging
from collections.abc import Collection
from typing import Any

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_redis._wrappers import (
    FTCreateWrapper,
    FTDropIndexWrapper,
    FTSearchWrapper,
    SearchIndexLoadWrapper,
    SearchIndexQueryWrapper,
    SearchIndexSearchWrapper,
)
from traceai_redis.package import _instruments
from traceai_redis.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
_MISSING = object()


class RedisInstrumentor(BaseInstrumentor):
    """Trace synchronous and asynchronous RedisVL and redis-py search APIs.

    Pass ``config=TraceConfig(...)`` to apply the same privacy policy as other
    traceAI instrumentors. Without it, TraceConfig reads the FI environment flags.
    """

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, kwargs.get("tracer_provider")),
            config=kwargs.get("config") or TraceConfig(),
        )
        self._originals = []
        try:
            try:
                import redisvl.index as redisvl_index
            except ImportError:
                logger.debug("RedisVL not available, skipping instrumentation")
            else:
                for class_name in ("SearchIndex", "AsyncSearchIndex"):
                    cls = getattr(redisvl_index, class_name, None)
                    if cls is not None:
                        for method, wrapper in (
                            ("search", SearchIndexSearchWrapper),
                            ("load", SearchIndexLoadWrapper),
                            ("query", SearchIndexQueryWrapper),
                        ):
                            self._patch(
                                cls,
                                method,
                                wrapper(
                                    tracer, is_async=class_name.startswith("Async")
                                ),
                            )

            from redis.commands.search import AsyncSearch, Search

            # AsyncSearch inherits Search before AsyncSearchCommands in its MRO.
            # Capture the async methods before a patch on Search can shadow them.
            for cls in (AsyncSearch, Search):
                for method, wrapper in (
                    ("search", FTSearchWrapper),
                    ("create_index", FTCreateWrapper),
                    ("dropindex", FTDropIndexWrapper),
                ):
                    self._patch(
                        cls, method, wrapper(tracer, is_async=cls is AsyncSearch)
                    )
        except BaseException:
            # BaseInstrumentor records an unsuccessful instrument() as inactive.
            # Undo earlier patches before re-raising, including interruption.
            self._uninstrument()
            raise

    def _patch(self, cls: type, method: str, wrapper: Any) -> None:
        # Preserve whether the attribute was inherited. Restoring an inherited
        # method with setattr leaves a stale class-level shadow behind.
        original = vars(cls).get(method, _MISSING)
        wrap_function_wrapper(cls, method, wrapper)
        self._originals.append((cls, method, original, vars(cls)[method], wrapper))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, method, original, installed, wrapper in reversed(self._originals):
            wrapper.enabled = False
            # Do not overwrite a patch installed by the application afterwards.
            # If ours remains underneath it, it is now an inert pass-through.
            if vars(cls).get(method) is installed:
                if original is _MISSING:
                    delattr(cls, method)
                else:
                    setattr(cls, method, original)
        self._originals.clear()


__all__ = ["RedisInstrumentor", "__version__"]
