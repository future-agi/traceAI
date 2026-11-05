import logging
from importlib import import_module
from importlib.metadata import version as package_version
from typing import Any, Collection

from fi_instrumentation import TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from packaging.version import Version
from traceai_crewai._wrappers import (
    _AsyncExecuteCoreWrapper,
    _AsyncKickoffWrapper,
    _AsyncNativeToolWrapper,
    _AsyncStructuredToolWrapper,
    _ExecuteCoreWrapper,
    _KickoffWrapper,
    _NativeToolWrapper,
    _StructuredToolWrapper,
    _ToolUseWrapper,
)
from traceai_crewai._standalone import (
    _AgentKickoffWrapper,
    _AsyncAgentKickoffWrapper,
    _AsyncFlowKickoffWrapper,
    _AsyncFlowMethodWrapper,
)
from traceai_crewai._context import _ContextPropagatingMethod
from traceai_crewai._privacy import _CrewAITraceConfig
from traceai_crewai._tracer import _CrewAITracer
from traceai_crewai.version import __version__
from wrapt import FunctionWrapper, wrap_function_wrapper

_instruments = ("crewai >= 0.41.1",)

logger = logging.getLogger(__name__)


class CrewAIInstrumentor(BaseInstrumentor):  # type: ignore
    __slots__ = (
        "_original_execute_core",
        "_original_kickoff",
        "_original_tool_use",
        "_optional_originals",
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
        self._tracer = _CrewAITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=_CrewAITraceConfig(config),
        )

        execute_core_wrapper = _ExecuteCoreWrapper(tracer=self._tracer)
        self._original_execute_core = getattr(
            import_module("crewai").Task, "_execute_core", None
        )
        wrap_function_wrapper(
            module="crewai",
            name="Task._execute_core",
            wrapper=execute_core_wrapper,
        )

        kickoff_wrapper = _KickoffWrapper(tracer=self._tracer)
        self._original_kickoff = getattr(import_module("crewai").Crew, "kickoff", None)
        wrap_function_wrapper(
            module="crewai",
            name="Crew.kickoff",
            wrapper=kickoff_wrapper,
        )

        use_wrapper = _ToolUseWrapper(tracer=self._tracer)
        self._original_tool_use = getattr(
            import_module("crewai.tools.tool_usage").ToolUsage, "_use", None
        )
        wrap_function_wrapper(
            module="crewai.tools.tool_usage",
            name="ToolUsage._use",
            wrapper=use_wrapper,
        )

        # These entry points do not exist on the supported 0.41.1 floor.
        # kickoff_async delegates to kickoff; wrapping both would double-count.
        self._optional_originals = []
        modern_runtime = Version(package_version("crewai")) >= Version("1.15.0")
        if not modern_runtime:
            # Legacy execute_async starts a bare Thread. Capture the caller's
            # context when it binds the target, even when tracing is suppressed.
            # 1.15+ already propagates context natively and needs no extra hook.
            task = import_module("crewai").Task
            original = vars(task).get("_execute_task_async")
            if original is not None:
                task._execute_task_async = _ContextPropagatingMethod(original)
                self._optional_originals.append((task, "_execute_task_async", original))
        for module_name, class_name, method_name, wrapper_type in (
            ("crewai", "Crew", "akickoff", _AsyncKickoffWrapper),
            ("crewai", "Task", "_aexecute_core", _AsyncExecuteCoreWrapper),
            ("crewai", "Agent", "kickoff", _AgentKickoffWrapper),
            ("crewai", "Agent", "kickoff_async", _AsyncAgentKickoffWrapper),
            (
                "crewai.tools.structured_tool",
                "CrewStructuredTool",
                "invoke",
                _StructuredToolWrapper,
            ),
            (
                "crewai.tools.structured_tool",
                "CrewStructuredTool",
                "ainvoke",
                _AsyncStructuredToolWrapper,
            ),
            ("crewai.tools.base_tool", "BaseTool", "run", _NativeToolWrapper),
            ("crewai.tools.base_tool", "BaseTool", "arun", _AsyncNativeToolWrapper),
            ("crewai.tools.base_tool", "Tool", "run", _NativeToolWrapper),
            ("crewai.tools.base_tool", "Tool", "arun", _AsyncNativeToolWrapper),
        ):
            # Earlier Agent.kickoff versions use LiteAgent instead of the
            # coroutine-factory contract covered by these public wrappers.
            if class_name == "Agent" and not modern_runtime:
                continue
            try:
                module = import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name != module_name:
                    raise
                continue
            cls = getattr(module, class_name, None)
            # Skip inherited methods: their defining class is wrapped separately,
            # and uninstrument must not add an override that did not exist.
            original = vars(cls).get(method_name) if cls is not None else None
            if original is not None:
                wrap_function_wrapper(
                    module_name,
                    f"{class_name}.{method_name}",
                    wrapper_type(tracer=self._tracer),
                )
                self._optional_originals.append((cls, method_name, original))

        # Flow.kickoff and akickoff delegate to kickoff_async. Recent CrewAI
        # exposes Flow through a subclass: wrap the actual defining owner once,
        # and restore it without leaving a synthetic override on the public API.
        try:
            flow_module = import_module("crewai.flow.flow")
        except ModuleNotFoundError as exc:
            if not exc.name or not (
                exc.name == "crewai.flow.flow"
                or "crewai.flow.flow".startswith(exc.name + ".")
            ):
                raise
        else:
            flow = getattr(flow_module, "Flow", None)
            hooks = [("kickoff_async", _AsyncFlowKickoffWrapper)]
            # 1.15+ executes nodes through the async runtime method, returning
            # (application result, event ID). Earlier Flow versions have a
            # different return contract; never infer it from a user's tuple.
            if modern_runtime:
                hooks.append(("_execute_method", _AsyncFlowMethodWrapper))
            for method_name, wrapper_type in hooks:
                for cls in flow.__mro__ if flow is not None else ():
                    original = vars(cls).get(method_name)
                    if original is not None:
                        setattr(
                            cls,
                            method_name,
                            FunctionWrapper(original, wrapper_type(tracer=self._tracer)),
                        )
                        self._optional_originals.append((cls, method_name, original))
                        break

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, method_name, original in self._optional_originals:
            setattr(cls, method_name, original)
        self._optional_originals = []
        if self._original_execute_core is not None:
            task_module = import_module("crewai")
            task_module.Task._execute_core = self._original_execute_core
            self._original_execute_core = None

        if self._original_kickoff is not None:
            crew_module = import_module("crewai")
            crew_module.Crew.kickoff = self._original_kickoff
            self._original_kickoff = None

        if self._original_tool_use is not None:
            tool_usage_module = import_module("crewai.tools.tool_usage")
            tool_usage_module.ToolUsage._use = self._original_tool_use
            self._original_tool_use = None
