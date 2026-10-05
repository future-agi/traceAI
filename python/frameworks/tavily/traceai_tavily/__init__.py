"""OpenTelemetry instrumentation for the Tavily Python client (tavily-python)."""

import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_tavily._wrappers import AsyncWrapper, SyncWrapper
from traceai_tavily.package import _instruments
from traceai_tavily.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# (module, class, wrapper type). Both methods are defined in each class body in
# tavily-python 0.8.4, so wrapping the class covers every instance, including
# the deprecated ``tavily.Client`` subclass.
_CLIENTS = (
    ("tavily.tavily", "TavilyClient", SyncWrapper),
    ("tavily.async_tavily", "AsyncTavilyClient", AsyncWrapper),
)
_METHODS = ("search", "extract")


class TavilyInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace ``search`` and ``extract`` on TavilyClient and AsyncTavilyClient.

    ``instrument()`` accepts ``tracer_provider`` and ``config`` (a
    ``fi_instrumentation.TraceConfig``; by default one read from the
    ``FI_HIDE_*`` environment variables).
    """

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError(
                "config must be a fi_instrumentation.TraceConfig, not {0}".format(
                    type(config).__name__
                )
            )
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )
        self._originals: Dict[Tuple[str, str, str], Any] = {}
        for module_name, class_name, wrapper_type in _CLIENTS:
            client = _vendor_class(module_name, class_name)
            if client is None:
                continue
            for method in _METHODS:
                original = vars(client).get(method)
                if original is None:
                    logger.warning(
                        "traceAI-tavily: %s.%s.%s not found; it is not traced",
                        module_name,
                        class_name,
                        method,
                    )
                    continue
                # wrapt 2.x rejects the keyword form; these are positional.
                wrap_function_wrapper(
                    module_name,
                    "{0}.{1}".format(class_name, method),
                    wrapper_type(tracer, method, bool(config.hide_inputs)),
                )
                self._originals[(module_name, class_name, method)] = original

    def _uninstrument(self, **kwargs: Any) -> None:
        originals = getattr(self, "_originals", None) or {}
        for (module_name, class_name, method), original in originals.items():
            client = _vendor_class(module_name, class_name)
            if client is not None:
                setattr(client, method, original)
        self._originals = {}


def _vendor_class(module_name: str, class_name: str) -> Any:
    """The vendor class, or None (logged) when a tavily-python release moved it."""
    try:
        return getattr(import_module(module_name), class_name)
    except (ImportError, AttributeError):
        logger.warning(
            "traceAI-tavily: %s.%s not found; it is not traced", module_name, class_name
        )
        return None


__all__ = ["TavilyInstrumentor", "__version__"]
