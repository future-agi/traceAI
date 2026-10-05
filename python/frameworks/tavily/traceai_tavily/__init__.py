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
    """Trace ``search`` and ``extract`` on TavilyClient and AsyncTavilyClient."""

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )
        self._originals: Dict[Tuple[str, str, str], Any] = {}
        for module_name, class_name, wrapper_type in _CLIENTS:
            module = import_module(module_name)
            client = getattr(module, class_name)
            for method in _METHODS:
                self._originals[(module_name, class_name, method)] = vars(client)[method]
                # wrapt 2.x rejects the keyword form; these are positional.
                wrap_function_wrapper(
                    module, "{0}.{1}".format(class_name, method), wrapper_type(tracer, method)
                )

    def _uninstrument(self, **kwargs: Any) -> None:
        for (module_name, class_name, method), original in self._originals.items():
            setattr(getattr(import_module(module_name), class_name), method, original)
        self._originals = {}


__all__ = ["TavilyInstrumentor", "__version__"]
