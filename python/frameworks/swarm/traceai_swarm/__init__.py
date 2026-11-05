import logging
from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from traceai_swarm._wrappers import (
    _SwarmRunWrapper,
)
from traceai_swarm.version import __version__
from wrapt import wrap_function_wrapper

_instruments = ("swarm >= 0.1.0",)

logger = logging.getLogger(__name__)


class SwarmInstrumentor(BaseInstrumentor):  # type: ignore
    __slots__ = (
        "_original_run",
        "_tracer",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        run_wrapper = _SwarmRunWrapper(tracer=self._tracer)
        self._original_run = getattr(import_module("swarm.core"), "Swarm.run", None)
        wrap_function_wrapper(
            module="swarm.core",
            name="Swarm.run",
            wrapper=run_wrapper,
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        if self._original_run is not None:
            swarm_module = import_module("swarm.core")
            swarm_module.Swarm.run = self._original_run
            self._original_run = None
