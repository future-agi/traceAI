import logging
from importlib import import_module
from typing import Any, Collection, Optional

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from traceai_autogen._legacy import safe_json_dumps, wrap_legacy
from traceai_autogen._v04_wrapper import (
    wrap_agent_on_messages,
    wrap_agent_on_messages_stream,
    wrap_team_run,
    wrap_tool_execution,
)
from traceai_autogen._attributes import AutoGenAttributes, AutoGenSpanKind, get_model_provider

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE_V04_AGENTS = "autogen_agentchat.agents"
_MODULE_V04_TEAMS = "autogen_agentchat.teams"
__version__ = "0.2.1"  # x-release-please-version

_instruments = ("autogen-agentchat>=0.4.0",)


def _is_v02_available() -> bool:
    try:
        return hasattr(import_module("autogen"), "ConversableAgent")
    except ImportError:
        return False


def _is_v04_available() -> bool:
    try:
        import_module(_MODULE_V04_AGENTS)
        return True
    except ImportError:
        return False


class AutogenInstrumentor(BaseInstrumentor):
    """Instrument AgentChat and, when installed, legacy ConversableAgent."""

    def __init__(self) -> None:
        super().__init__()
        # BaseInstrumentor is a singleton: constructing it again must not discard
        # the originals needed by uninstrument().
        if hasattr(self, "_tracer"):
            return
        self._tracer = None
        self._patches = {}
        self._original_generate = None
        self._original_initiate_chat = None
        self._original_execute_function = None
        self._v04_original_on_messages = {}
        self._v04_original_team_run = {}
        self._v04_original_team_run_stream = {}

    _safe_json_dumps = staticmethod(safe_json_dumps)

    def instrumentation_dependencies(self) -> Collection[str]:
        # Requirements name distributions, not import modules ("autogen" is not
        # the distribution providing Microsoft's legacy 0.2 SDK).
        return _instruments

    def _patch(self, cls, method, wrapper, originals=None):
        # Patch the defining class once. Setting inherited methods on every team
        # subclass double-wraps calls and changes the MRO after uninstrument().
        owner = next((base for base in cls.__mro__ if method in vars(base)), None)
        if owner is None or (owner, method) in self._patches:
            return
        original = vars(owner)[method]
        self._patches[owner, method] = original
        if originals is not None:
            originals[owner] = original
        setattr(owner, method, wrapper(original))

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        config = kwargs.get("config") or TraceConfig()
        assert isinstance(config, TraceConfig)
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )

        if _is_v02_available():
            legacy = import_module("autogen").ConversableAgent
            self._original_generate = legacy.generate_reply
            self._original_initiate_chat = legacy.initiate_chat
            self._original_execute_function = legacy.execute_function
            for method in ("generate_reply", "initiate_chat", "execute_function",
                           "a_generate_reply", "a_initiate_chat", "a_execute_function"):
                self._patch(legacy, method, lambda original, method=method:
                            wrap_legacy(original, self._tracer, method, config))

        try:
            agents = import_module(_MODULE_V04_AGENTS)
            for cls in vars(agents).values():
                if not isinstance(cls, type) or not issubclass(cls, agents.BaseChatAgent):
                    continue
                self._patch(cls, "on_messages",
                            lambda original: wrap_agent_on_messages(original, self._tracer, config),
                            self._v04_original_on_messages)
                self._patch(cls, "on_messages_stream",
                            lambda original: wrap_agent_on_messages_stream(original, self._tracer, config))
        except ImportError:
            logger.debug("AutoGen AgentChat agents not available")

        try:
            teams = import_module(_MODULE_V04_TEAMS)
            for name in ("BaseGroupChat", "RoundRobinGroupChat", "SelectorGroupChat",
                         "Swarm", "MagenticOneGroupChat"):
                cls = getattr(teams, name, None)
                if not isinstance(cls, type):
                    continue
                self._patch(cls, "run",
                            lambda original: wrap_team_run(original, self._tracer, "run", config),
                            self._v04_original_team_run)
                self._patch(cls, "run_stream",
                            lambda original: wrap_team_run(original, self._tracer, "run_stream", config),
                            self._v04_original_team_run_stream)
        except ImportError:
            logger.debug("AutoGen AgentChat teams not available")

    def _uninstrument(self, **kwargs: Any) -> None:
        for (cls, method), original in reversed(list(self._patches.items())):
            setattr(cls, method, original)
        self._patches.clear()
        self._original_generate = None
        self._original_initiate_chat = None
        self._original_execute_function = None
        self._v04_original_on_messages.clear()
        self._v04_original_team_run.clear()
        self._v04_original_team_run_stream.clear()
        self._tracer = None


def instrument_autogen(
    tracer_provider: Optional[Any] = None,
    config: Optional[TraceConfig] = None,
) -> AutogenInstrumentor:
    """Instrument AgentChat and installed legacy ConversableAgent classes."""
    instrumentor = AutogenInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider, config=config)
    return instrumentor


__all__ = [
    "AutogenInstrumentor",
    "instrument_autogen",
    "AutoGenAttributes",
    "AutoGenSpanKind",
    "get_model_provider",
    "wrap_tool_execution",
]
