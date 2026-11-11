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


def _attachment_manager(kwargs: Any) -> Any:
    """The shared manager passed as ``attachments`` (or ``attachment_manager``), if it can admit.

    A missing, disabled or pre-attachments core leaves tracing exactly as before.
    """
    manager = kwargs.get("attachments")
    if manager is None:
        manager = kwargs.get("attachment_manager")
    if manager is None:
        return None
    try:
        from fi_instrumentation.attachments.media_capture import active
    except ImportError:
        return None
    return manager if active(manager) else None


class OpenAIAgentsInstrumentor(BaseInstrumentor):  # type: ignore
    """
    An instrumentor for openai-agents
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

        # The provider-owned shared manager, passed as ``attachments`` (auto also passes
        # ``attachment_manager``). Nothing is captured unless an enabled manager is supplied.
        processor = FiTracingProcessor(
            cast(Tracer, tracer),
            attachments=_attachment_manager(kwargs),
            trace_config=config,
            attachment_capture=kwargs.get("attachment_capture"),
        )
        if exclusive_processor:
            from agents import set_trace_processors

            set_trace_processors([processor])
        else:
            from agents import add_trace_processor

            add_trace_processor(processor)

    def _uninstrument(self, **kwargs: Any) -> None:
        # TODO : OpenAI Agents does not support uninstrumentation currently
        pass
