import json
import logging
from importlib import import_module
from typing import Any, Callable, Collection, Dict, Optional

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import Status, StatusCode
from traceai_autogen._v04_wrapper import (
    wrap_agent_on_messages,
    wrap_team_run,
    wrap_tool_execution,
)
from traceai_autogen._attributes import AutoGenAttributes, AutoGenSpanKind, get_model_provider

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE_V04_AGENTS = "autogen_agentchat.agents"
_MODULE_V04_TEAMS = "autogen_agentchat.teams"
__version__ = "0.2.0"

_instruments = ("autogen-agentchat>=0.4.0",)


class AutogenInstrumentor(BaseInstrumentor):
    """
    An instrumentor for AutoGen v0.4+ (AgentChat).

    Instruments:
    - autogen_agentchat.agents: AssistantAgent, BaseChatAgent
    - autogen_agentchat.teams: RoundRobinGroupChat, SelectorGroupChat, etc.
    """

    __slots__ = (
        "_tracer",
        "_v04_original_on_messages",
        "_v04_original_team_run",
        "_v04_original_team_run_stream",
        "_v04_instrumented_classes",
    )

    def __init__(self) -> None:
        super().__init__()
        self._v04_original_on_messages: Dict[type, Callable] = {}
        self._v04_original_team_run: Dict[type, Callable] = {}
        self._v04_original_team_run_stream: Dict[type, Callable] = {}
        self._v04_instrumented_classes: list = []
        self._tracer = None

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

        raw_tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        # Instrument agent classes
        try:
            agents_module = import_module(_MODULE_V04_AGENTS)

            if hasattr(agents_module, "BaseChatAgent"):
                BaseChatAgent = agents_module.BaseChatAgent
                if hasattr(BaseChatAgent, "on_messages"):
                    self._v04_original_on_messages[BaseChatAgent] = BaseChatAgent.on_messages
                    BaseChatAgent.on_messages = wrap_agent_on_messages(
                        BaseChatAgent.on_messages, raw_tracer
                    )
                    self._v04_instrumented_classes.append(("BaseChatAgent", BaseChatAgent))
                    logger.debug("Instrumented BaseChatAgent.on_messages")

            if hasattr(agents_module, "AssistantAgent"):
                AssistantAgent = agents_module.AssistantAgent
                self._v04_instrumented_classes.append(("AssistantAgent", AssistantAgent))
                logger.debug("AssistantAgent tracked (inherits BaseChatAgent instrumentation)")

        except ImportError as e:
            logger.debug(f"Could not import autogen_agentchat.agents: {e}")

        # Instrument team classes
        try:
            teams_module = import_module(_MODULE_V04_TEAMS)

            team_classes = [
                "BaseGroupChat",
                "RoundRobinGroupChat",
                "SelectorGroupChat",
                "Swarm",
                "MagenticOneGroupChat",
            ]

            for class_name in team_classes:
                if hasattr(teams_module, class_name):
                    TeamClass = getattr(teams_module, class_name)

                    if hasattr(TeamClass, "run"):
                        if TeamClass not in self._v04_original_team_run:
                            self._v04_original_team_run[TeamClass] = TeamClass.run
                            TeamClass.run = wrap_team_run(TeamClass.run, raw_tracer, "run")
                            logger.debug(f"Instrumented {class_name}.run")

                    if hasattr(TeamClass, "run_stream"):
                        if TeamClass not in self._v04_original_team_run_stream:
                            self._v04_original_team_run_stream[TeamClass] = TeamClass.run_stream
                            TeamClass.run_stream = wrap_team_run(
                                TeamClass.run_stream, raw_tracer, "run_stream"
                            )
                            logger.debug(f"Instrumented {class_name}.run_stream")

                    self._v04_instrumented_classes.append((class_name, TeamClass))

        except ImportError as e:
            logger.debug(f"Could not import autogen_agentchat.teams: {e}")

        logger.info(f"AutoGen v0.4 instrumentation complete. "
                   f"Instrumented {len(self._v04_instrumented_classes)} classes.")

    def _uninstrument(self, **kwargs: Any) -> None:
        """Restore original behavior."""
        for agent_class, original_method in self._v04_original_on_messages.items():
            if hasattr(agent_class, "on_messages"):
                agent_class.on_messages = original_method

        for team_class, original_method in self._v04_original_team_run.items():
            if hasattr(team_class, "run"):
                team_class.run = original_method

        for team_class, original_method in self._v04_original_team_run_stream.items():
            if hasattr(team_class, "run_stream"):
                team_class.run_stream = original_method

        self._v04_original_on_messages.clear()
        self._v04_original_team_run.clear()
        self._v04_original_team_run_stream.clear()
        self._v04_instrumented_classes.clear()


def instrument_autogen(
    tracer_provider: Optional[Any] = None,
    config: Optional[TraceConfig] = None,
) -> AutogenInstrumentor:
    """Convenience function to instrument AutoGen v0.4+.

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider
        config: Optional trace configuration

    Returns:
        The instrumentor instance

    Example:
        >>> from traceai_autogen import instrument_autogen
        >>> instrumentor = instrument_autogen()
        >>> from autogen_agentchat.agents import AssistantAgent
        >>> from autogen_agentchat.teams import RoundRobinGroupChat
        >>> agent = AssistantAgent("assistant", ...)
        >>> team = RoundRobinGroupChat([agent], ...)
        >>> await team.run(task="...")  # Automatically traced
    """
    instrumentor = AutogenInstrumentor()
    kwargs = {}
    if tracer_provider:
        kwargs["tracer_provider"] = tracer_provider
    if config:
        kwargs["config"] = config
    instrumentor.instrument(**kwargs)
    return instrumentor


__all__ = [
    "AutogenInstrumentor",
    "instrument_autogen",
    "AutoGenAttributes",
    "AutoGenSpanKind",
    "get_model_provider",
    "wrap_tool_execution",
]
