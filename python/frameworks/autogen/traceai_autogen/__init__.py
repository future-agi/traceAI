import json
import logging
from importlib import import_module
from typing import Any, Callable, Collection, Dict, Optional, Union

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import Link, SpanContext, Status, StatusCode
from traceai_autogen.utils import _to_dict
from traceai_autogen._v04_wrapper import (
    wrap_agent_on_messages,
    wrap_team_run,
    wrap_tool_execution,
)
from traceai_autogen._attributes import AutoGenAttributes, AutoGenSpanKind, get_model_provider

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "autogen"
_MODULE_V04_AGENTS = "autogen_agentchat.agents"
_MODULE_V04_TEAMS = "autogen_agentchat.teams"
__version__ = "0.1.0"


def _is_v04_available() -> bool:
    """Check if AutoGen v0.4 (autogen_agentchat) is available."""
    try:
        import_module(_MODULE_V04_AGENTS)
        return True
    except ImportError:
        return False


def _is_v02_available() -> bool:
    """Check if AutoGen v0.2 (autogen) is available."""
    try:
        import_module(_MODULE)
        return True
    except ImportError:
        return False


class AutogenInstrumentor(BaseInstrumentor):
    """
    An instrumentor for autogen (supports both v0.2 and v0.4).

    AutoGen v0.2 (legacy):
    - ConversableAgent with generate_reply, initiate_chat, execute_function

    AutoGen v0.4 (AgentChat):
    - autogen_agentchat.agents: AssistantAgent, BaseChatAgent
    - autogen_agentchat.teams: RoundRobinGroupChat, SelectorGroupChat, etc.
    """

    __slots__ = (
        "_original_generate",
        "_original_initiate_chat",
        "_original_execute_function",
        # v0.4 originals
        "_v04_original_on_messages",
        "_v04_original_team_run",
        "_v04_original_team_run_stream",
        "_v04_instrumented_classes",
    )

    def __init__(self) -> None:
        super().__init__()
        # v0.2 originals
        self._original_generate: Optional[Callable[..., Any]] = None
        self._original_initiate_chat: Optional[Callable[..., Any]] = None
        self._original_execute_function: Optional[Callable[..., Any]] = None
        # v0.4 originals - stored as dict mapping class -> original method
        self._v04_original_on_messages: Dict[type, Callable] = {}
        self._v04_original_team_run: Dict[type, Callable] = {}
        self._v04_original_team_run_stream: Dict[type, Callable] = {}
        self._v04_instrumented_classes: list = []
        self.tracer = None

    def _safe_json_dumps(self, obj: Any) -> str:
        try:
            return json.dumps(obj)
        except (TypeError, ValueError):
            return json.dumps(str(obj))

    def instrumentation_dependencies(self) -> Collection[str]:
        # Support either v0.2 (autogen) or v0.4 (autogen_agentchat)
        deps = []
        if _is_v02_available():
            deps.append(_MODULE)
        if _is_v04_available():
            deps.append("autogen_agentchat")
        return deps if deps else [_MODULE]  # Fallback to v0.2 module name

    def _instrument(self, **kwargs: Any) -> None:
        # Get tracer provider and config
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)

        # Create tracer
        self.tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        # Instrument v0.2 if available
        if not _is_v02_available():
            logger.debug("AutoGen v0.2 not available, skipping v0.2 instrumentation")
            # Still try v0.4
            self._instrument_v04(tracer_provider)
            return

        autogen = import_module(_MODULE)
        ConversableAgent = autogen.ConversableAgent

        # Save original methods
        self._original_generate = ConversableAgent.generate_reply
        self._original_initiate_chat = ConversableAgent.initiate_chat
        self._original_execute_function = ConversableAgent.execute_function

        instrumentor = self

        def wrapped_generate(
            agent_self: Any,
            messages: Optional[Any] = None,
            sender: Optional[str] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                current_span = trace_api.get_current_span()
                current_context: SpanContext = current_span.get_span_context()

                with instrumentor.tracer.start_as_current_span(
                    agent_self.__class__.__name__,
                    context=trace_api.set_span_in_context(current_span),
                    links=[Link(current_context)],
                ) as span:
                    span.set_attribute(SpanAttributes.GEN_AI_SPAN_KIND, "AGENT")
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(messages),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(messages),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_MIME_TYPE, "application/json"
                    )
                    span.set_attribute("agent.type", agent_self.__class__.__name__)

                    response = instrumentor._original_generate(
                        agent_self, messages=messages, sender=sender, **kwargs
                    )

                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(response),
                    )
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(response),
                    )
                    span.set_attribute(
                        SpanAttributes.OUTPUT_MIME_TYPE, "application/json"
                    )

                    span.set_status(Status(StatusCode.OK))
                    return response
            except Exception as e:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(e)
                raise

        def wrapped_initiate_chat(
            agent_self: Any, recipient: Any, *args: Any, **kwargs: Any
        ) -> Any:
            try:
                message = kwargs.get("message", args[0] if args else None)
                current_span = trace_api.get_current_span()
                current_context: SpanContext = current_span.get_span_context()

                with instrumentor.tracer.start_as_current_span(
                    "Autogen",
                    context=trace_api.set_span_in_context(current_span),
                    links=[Link(current_context)],
                ) as span:
                    span.set_attribute(SpanAttributes.GEN_AI_SPAN_KIND, "AGENT")
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(
                            {
                                "args": args,
                                **kwargs,
                            }
                        ),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(message),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_MIME_TYPE, "application/json"
                    )

                    result = instrumentor._original_initiate_chat(
                        agent_self, recipient, *args, **kwargs
                    )

                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(_to_dict(result)),
                    )
                    if hasattr(result, "chat_history") and result.chat_history:
                        last_message = result.chat_history[-1]["content"]
                        span.set_attribute(
                            SpanAttributes.OUTPUT_VALUE,
                            instrumentor._safe_json_dumps(last_message),
                        )
                    else:
                        span.set_attribute(
                            SpanAttributes.OUTPUT_VALUE,
                            instrumentor._safe_json_dumps(result),
                        )

                    span.set_attribute(
                        SpanAttributes.OUTPUT_MIME_TYPE, "application/json"
                    )

                    span.set_status(Status(StatusCode.OK))
                    return result
            except Exception as e:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(e)
                raise

        def wrapped_execute_function(
            agent_self: Any,
            func_call: Union[str, Dict[str, Any]],
            call_id: Optional[str] = None,
            verbose: bool = False,
        ) -> Any:
            try:
                current_span = trace_api.get_current_span()
                current_context: SpanContext = current_span.get_span_context()

                # Handle both dictionary and string inputs
                if isinstance(func_call, str):
                    function_name = func_call
                    func_call = {"name": function_name}
                else:
                    function_name = func_call.get("name", "unknown")

                with instrumentor.tracer.start_as_current_span(
                    f"{function_name}",
                    context=trace_api.set_span_in_context(current_span),
                    links=[Link(current_context)],
                ) as span:
                    span.set_attribute(SpanAttributes.GEN_AI_SPAN_KIND, "TOOL")
                    span.set_attribute(SpanAttributes.GEN_AI_TOOL_NAME, function_name)

                    # Record input
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(func_call),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(func_call),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_MIME_TYPE, "application/json"
                    )

                    # If the agent stores a function map, you can store annotations
                    if hasattr(agent_self, "_function_map"):
                        function_map = getattr(agent_self, "_function_map", {})
                        if function_name in function_map:
                            func = function_map[function_name]
                            if hasattr(func, "__annotations__"):
                                span.set_attribute(
                                    SpanAttributes.TOOL_PARAMETERS,
                                    instrumentor._safe_json_dumps(func.__annotations__),
                                )

                    # Record function call details
                    if isinstance(func_call, dict):
                        # Record function arguments
                        if "arguments" in func_call:
                            span.set_attribute(
                                SpanAttributes.TOOL_CALL_FUNCTION_ARGUMENTS,
                                instrumentor._safe_json_dumps(func_call["arguments"]),
                            )

                        # Record function name
                        span.set_attribute(
                            SpanAttributes.TOOL_CALL_FUNCTION_NAME, function_name
                        )

                    # Execute function
                    result = instrumentor._original_execute_function(
                        agent_self, func_call, call_id=call_id, verbose=verbose
                    )

                    # Record output
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(result),
                    )
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(result),
                    )
                    span.set_attribute(
                        SpanAttributes.OUTPUT_MIME_TYPE, "application/json"
                    )

                    span.set_status(Status(StatusCode.OK))
                    return result

            except Exception as e:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(e)
                raise

        # Replace methods on ConversableAgent with wrapped versions
        ConversableAgent.generate_reply = wrapped_generate
        ConversableAgent.initiate_chat = wrapped_initiate_chat
        ConversableAgent.execute_function = wrapped_execute_function

        # Also instrument v0.4 if available
        self._instrument_v04(tracer_provider)

    def _instrument_v04(self, tracer_provider) -> None:
        """Instrument AutoGen v0.4 (AgentChat) classes."""
        if not _is_v04_available():
            logger.debug("AutoGen v0.4 not available, skipping v0.4 instrumentation")
            return

        logger.info("Instrumenting AutoGen v0.4 (AgentChat)")

        # Get the raw tracer for v0.4 wrappers
        raw_tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        # Import agent classes
        try:
            agents_module = import_module(_MODULE_V04_AGENTS)

            # Instrument BaseChatAgent (base class)
            if hasattr(agents_module, "BaseChatAgent"):
                BaseChatAgent = agents_module.BaseChatAgent
                if hasattr(BaseChatAgent, "on_messages"):
                    self._v04_original_on_messages[BaseChatAgent] = BaseChatAgent.on_messages
                    BaseChatAgent.on_messages = wrap_agent_on_messages(
                        BaseChatAgent.on_messages, raw_tracer
                    )
                    self._v04_instrumented_classes.append(("BaseChatAgent", BaseChatAgent))
                    logger.debug("Instrumented BaseChatAgent.on_messages")

            # Instrument AssistantAgent specifically for additional attributes
            if hasattr(agents_module, "AssistantAgent"):
                AssistantAgent = agents_module.AssistantAgent
                # AssistantAgent inherits from BaseChatAgent, so on_messages is already wrapped
                # But we track it for potential agent-specific enhancements
                self._v04_instrumented_classes.append(("AssistantAgent", AssistantAgent))
                logger.debug("AssistantAgent tracked (inherits BaseChatAgent instrumentation)")

        except ImportError as e:
            logger.debug(f"Could not import autogen_agentchat.agents: {e}")

        # Import team classes
        try:
            teams_module = import_module(_MODULE_V04_TEAMS)

            # Team classes to instrument
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

                    # Wrap run method
                    if hasattr(TeamClass, "run"):
                        original_run = TeamClass.run
                        # Only wrap if not already wrapped (for base class inheritance)
                        if TeamClass not in self._v04_original_team_run:
                            self._v04_original_team_run[TeamClass] = original_run
                            TeamClass.run = wrap_team_run(original_run, raw_tracer, "run")
                            logger.debug(f"Instrumented {class_name}.run")

                    # Wrap run_stream method
                    if hasattr(TeamClass, "run_stream"):
                        original_run_stream = TeamClass.run_stream
                        if TeamClass not in self._v04_original_team_run_stream:
                            self._v04_original_team_run_stream[TeamClass] = original_run_stream
                            TeamClass.run_stream = wrap_team_run(
                                original_run_stream, raw_tracer, "run_stream"
                            )
                            logger.debug(f"Instrumented {class_name}.run_stream")

                    self._v04_instrumented_classes.append((class_name, TeamClass))

        except ImportError as e:
            logger.debug(f"Could not import autogen_agentchat.teams: {e}")

        logger.info(f"AutoGen v0.4 instrumentation complete. "
                   f"Instrumented {len(self._v04_instrumented_classes)} classes.")

    def _uninstrument_v04(self) -> None:
        """Restore original v0.4 methods."""
        # Restore agent on_messages
        for agent_class, original_method in self._v04_original_on_messages.items():
            if hasattr(agent_class, "on_messages"):
                agent_class.on_messages = original_method
                logger.debug(f"Restored {agent_class.__name__}.on_messages")

        # Restore team run
        for team_class, original_method in self._v04_original_team_run.items():
            if hasattr(team_class, "run"):
                team_class.run = original_method
                logger.debug(f"Restored {team_class.__name__}.run")

        # Restore team run_stream
        for team_class, original_method in self._v04_original_team_run_stream.items():
            if hasattr(team_class, "run_stream"):
                team_class.run_stream = original_method
                logger.debug(f"Restored {team_class.__name__}.run_stream")

        # Clear stored references
        self._v04_original_on_messages.clear()
        self._v04_original_team_run.clear()
        self._v04_original_team_run_stream.clear()
        self._v04_instrumented_classes.clear()

    def _uninstrument(self, **kwargs: Any) -> None:
        """Restore original behavior."""
        # Uninstrument v0.2
        if (
            self._original_generate
            and self._original_initiate_chat
            and self._original_execute_function
        ):
            try:
                # Import autogen module safely to avoid circular imports
                autogen = import_module(_MODULE)
                ConversableAgent = autogen.ConversableAgent

                ConversableAgent.generate_reply = self._original_generate
                ConversableAgent.initiate_chat = self._original_initiate_chat
                ConversableAgent.execute_function = self._original_execute_function
            except ImportError:
                pass

            self._original_generate = None
            self._original_initiate_chat = None
            self._original_execute_function = None

        # Uninstrument v0.4
        self._uninstrument_v04()


def instrument_autogen(
    tracer_provider: Optional[Any] = None,
    config: Optional[TraceConfig] = None,
) -> AutogenInstrumentor:
    """Convenience function to instrument AutoGen.

    Supports both AutoGen v0.2 (autogen package) and v0.4 (autogen_agentchat).

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider
        config: Optional trace configuration

    Returns:
        The instrumentor instance

    Example:
        >>> from traceai_autogen import instrument_autogen
        >>> instrumentor = instrument_autogen()

        # For v0.2:
        >>> from autogen import ConversableAgent
        >>> agent = ConversableAgent("assistant", ...)
        >>> agent.initiate_chat(...)  # Automatically traced

        # For v0.4:
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


# Re-export useful items
__all__ = [
    "AutogenInstrumentor",
    "instrument_autogen",
    "AutoGenAttributes",
    "AutoGenSpanKind",
    "get_model_provider",
    "wrap_tool_execution",  # Useful for manually wrapping custom tools
]
