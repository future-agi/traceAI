import logging
from typing import Any, Collection, cast

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import Tracer
from traceai_openai_agents.package import _instruments
from traceai_openai_agents.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class OpenAIAgentsInstrumentor(BaseInstrumentor):  # type: ignore
    """Bridge the official OpenAI Agents tracing processor to OpenTelemetry.

    ``instrument(tracer_provider=..., config=TraceConfig(...))`` uses the
    supplied provider for span creation and flushing. By default it replaces
    all SDK trace processors, including OpenAI's exporter. Set
    ``exclusive_processor=False`` to keep existing export destinations.
    The application retains ownership of the provider's shutdown lifecycle.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if (exclusive_processor := kwargs.get("exclusive_processor")) is None:
            exclusive_processor = True
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        from traceai_openai_agents._processor import FiTracingProcessor

        processor = FiTracingProcessor(cast(Tracer, tracer), tracer_provider=tracer_provider)
        if exclusive_processor:
            from agents import set_trace_processors

            set_trace_processors([processor])
        else:
            from agents import add_trace_processor

            add_trace_processor(processor)

    def _uninstrument(self, **kwargs: Any) -> None:
        # TODO : OpenAI Agents does not support uninstrumentation currently
        pass
