import logging
from typing import Any, Collection, Dict, Iterator, List, Tuple, cast

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import (  # type: ignore[attr-defined]
    BaseInstrumentor,
)
from opentelemetry.trace import Span, Tracer, get_current_span
from opentelemetry.util._decorator import _agnosticcontextmanager
from wrapt import resolve_path, wrap_function_wrapper

from fi_instrumentation import FITracer, TraceConfig
from traceai_google_adk.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("google-adk >= 1.2.1",)


class GoogleADKInstrumentor(BaseInstrumentor):  # type: ignore
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)

        self._tracer = cast(
            Tracer,
            FITracer(
                trace_api.get_tracer(__name__, __version__, tracer_provider),
                config=config,
            ),
        )

        from google.adk.agents import BaseAgent
        from google.adk.runners import Runner

        from traceai_google_adk._wrappers import (
            _BaseAgentRunAsync,
            _RunnerRunAsync,
        )

        # Store original methods for cleanup during uninstrumentation
        self._originals: List[Tuple[Any, Any, Any]] = []
        method_wrappers: Dict[Any, Any] = {
            Runner.run_async: _RunnerRunAsync(self._tracer),
            BaseAgent.run_async: _BaseAgentRunAsync(self._tracer),
        }

        # Wrap each method with its corresponding tracer
        for method, wrapper in method_wrappers.items():
            module, name = method.__module__, method.__qualname__
            self._originals.append(resolve_path(module, name))
            wrap_function_wrapper(module, name, wrapper)

        self._patch_trace_call_llm()
        self._patch_trace_tool_call()
        self._disable_existing_tracers()

    def _uninstrument(self, **kwargs: Any) -> None:
        self._unpatch_trace_call_llm()
        self._unpatch_trace_tool_call()
        self._restore_existing_tracers()

        # Restore all wrapped methods to their original state
        for parent, attribute, original in getattr(self, "_originals", ()):
            setattr(parent, attribute, original)

    def _patch_trace_call_llm(self) -> None:
        """Patch the LLM call tracing functionality to use our tracer."""
        from traceai_google_adk._wrappers import _TraceCallLlm

        target = _resolve_trace_call_llm_module()
        self._llm_trace_module = target
        self._llm_original_tracer = target.tracer
        self._llm_original_trace = target.trace_call_llm
        setattr(target, "tracer", self._tracer)
        setattr(
            target,
            "trace_call_llm",
            _TraceCallLlm(self._tracer)(target.trace_call_llm),
        )

    def _unpatch_trace_call_llm(self) -> None:
        """Restore the original LLM call tracing functionality."""
        if target := getattr(self, "_llm_trace_module", None):
            setattr(target, "trace_call_llm", self._llm_original_trace)
            setattr(target, "tracer", self._llm_original_tracer)

    def _patch_trace_tool_call(self) -> None:
        """Patch tool call tracing to use our tracer."""
        from traceai_google_adk._wrappers import _TraceToolCall

        target = _resolve_trace_tool_call_module()
        # Pre-1.32 the target's local `tracer` is single-purpose so we own it.
        # On 1.32+ it's the shared module tracer; the selective proxy installed
        # by _disable_existing_tracers handles routing instead.
        if _adk_version() < (1, 32, 0):
            setattr(target, "tracer", self._tracer)
        setattr(
            target,
            "trace_tool_call",
            _TraceToolCall(self._tracer)(target.trace_tool_call),
        )

    def _unpatch_trace_tool_call(self) -> None:
        """Restore the original tool call tracing functionality."""
        target = _resolve_trace_tool_call_module()

        if callable(
            original := getattr(target.trace_tool_call, "__wrapped__", None),
        ):
            setattr(target, "trace_tool_call", original)

        if _adk_version() < (1, 32, 0):
            from google.adk.telemetry import tracer

            setattr(target, "tracer", tracer)

    def _disable_existing_tracers(self) -> None:
        """Wrap ADK's internal tracers so they don't duplicate our spans."""
        from google.adk.runners import (  # type: ignore[attr-defined]
            tracer,  # pyright: ignore[reportPrivateImportUsage]
        )

        if isinstance(tracer, Tracer):
            from google.adk import runners

            setattr(runners, "tracer", _PassthroughTracer(tracer))

        # base_agent.tracer was dropped in 1.32 — only relevant on older ADK.
        if _adk_version() < (1, 32, 0):
            from google.adk.agents.base_agent import (  # type: ignore[attr-defined,unused-ignore]
                tracer as base_agent_tracer,  # pyright: ignore[reportPrivateImportUsage]
            )

            if isinstance(base_agent_tracer, Tracer):
                from google.adk.agents import base_agent

                setattr(base_agent, "tracer", _PassthroughTracer(base_agent_tracer))

        if _adk_version() >= (1, 32, 0):
            # 1.32 routes execute_tool / invoke_agent / generate_content
            # through one shared tracer. The selective proxy lets the tool
            # family produce real spans; the rest is handled by our outer
            # wrappers, so it's passed through.
            from google.adk.telemetry import (  # type: ignore[attr-defined,import-not-found,unused-ignore]
                tracing as adk_tracing,  # type: ignore[attr-defined,unused-ignore]
            )

            if isinstance(adk_tracing.tracer, Tracer):
                setattr(
                    adk_tracing,
                    "tracer",
                    _SelectiveExecuteToolTracer(adk_tracing.tracer, self._tracer),
                )
            # Parallel-call spans capture their tracer at import as well.
            target = _resolve_merged_tool_calls_module()
            functions_tracer = getattr(target, "tracer", None)
            if isinstance(functions_tracer, Tracer):
                setattr(
                    target,
                    "tracer",
                    _SelectiveExecuteToolTracer(functions_tracer, self._tracer),
                )
        elif _adk_version() >= (1, 15, 0):
            from google.adk.telemetry import (  # type: ignore[attr-defined,import-not-found,unused-ignore]
                tracing as adk_tracing,  # type: ignore[attr-defined,unused-ignore]
            )

            if isinstance(adk_tracing.tracer, Tracer):
                setattr(adk_tracing, "tracer", _PassthroughTracer(adk_tracing.tracer))

    def _restore_existing_tracers(self) -> None:
        """Restore original tracers that were disabled during instrumentation."""
        from google.adk.runners import (  # type: ignore[attr-defined]
            tracer,  # pyright: ignore[reportPrivateImportUsage]
        )

        if isinstance(original := getattr(tracer, "__wrapped__"), Tracer):
            from google.adk import runners

            setattr(runners, "tracer", original)

        if _adk_version() < (1, 32, 0):
            from google.adk.agents.base_agent import (  # type: ignore[attr-defined,unused-ignore]
                tracer as base_agent_tracer,  # pyright: ignore[reportPrivateImportUsage]
            )

            if isinstance(
                original := getattr(base_agent_tracer, "__wrapped__"), Tracer
            ):
                from google.adk.agents import base_agent

                setattr(base_agent, "tracer", original)

        if _adk_version() >= (1, 15, 0):
            from google.adk.telemetry import (  # type: ignore[attr-defined,import-not-found,unused-ignore]
                tracing as adk_tracing,  # type: ignore[attr-defined,unused-ignore]
            )

            if isinstance(
                original := getattr(adk_tracing.tracer, "__wrapped__", None), Tracer
            ):
                setattr(adk_tracing, "tracer", original)

        if _adk_version() >= (1, 32, 0):
            target = _resolve_merged_tool_calls_module()
            functions_tracer = getattr(target, "tracer", None)
            if isinstance(
                original := getattr(functions_tracer, "__wrapped__", None), Tracer
            ):
                setattr(target, "tracer", original)


class _PassthroughTracer(wrapt.ObjectProxy):  # type: ignore[misc]
    """Yields the current span instead of opening a new one.

    Wraps an ADK-internal tracer whose spans would duplicate work an outer
    wrapper already covers.
    """

    @_agnosticcontextmanager
    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Iterator[Span]:
        yield get_current_span()

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        # A caller owns and ends a span returned by start_span. Do not give it
        # the outer wrapper's recording span to end.
        return trace_api.NonRecordingSpan(get_current_span().get_span_context())


class _SelectiveExecuteToolTracer(wrapt.ObjectProxy):  # type: ignore[misc]
    """Forwards ``execute_tool *`` spans to our tracer; passes the rest through.

    On ADK 1.32+ a single shared tracer drives three span families
    (execute_tool, invoke_agent, generate_content). The agent and LLM
    families are already covered by our outer wrappers, so only the tool
    family should produce real spans here.
    """

    def __init__(self, wrapped: Tracer, fi_tracer: Tracer) -> None:
        super().__init__(wrapped)
        self._self_fi_tracer = fi_tracer

    @_agnosticcontextmanager
    def start_as_current_span(
        self, name: str, *args: Any, **kwargs: Any
    ) -> Iterator[Span]:
        if isinstance(name, str) and name.startswith("execute_tool"):
            with self._self_fi_tracer.start_as_current_span(
                name, *args, **kwargs
            ) as span:
                yield span
            return
        yield get_current_span()

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> Span:
        if isinstance(name, str) and name.startswith("execute_tool"):
            return self._self_fi_tracer.start_span(name, *args, **kwargs)
        return trace_api.NonRecordingSpan(get_current_span().get_span_context())


def _resolve_trace_call_llm_module() -> Any:
    """Find the binding used by ADK's model-call implementation."""
    from google.adk.flows.llm_flows import base_llm_flow

    if hasattr(base_llm_flow, "trace_call_llm"):
        return base_llm_flow
    # ADK's modular flow layout moved the imported function and tracer here.
    from google.adk.flows.llm_flows.core import _model_call

    return _model_call


def _resolve_merged_tool_calls_module() -> Any:
    """Find the captured tracer used for parallel tool-call spans."""
    from google.adk.flows.llm_flows import functions

    if hasattr(functions, "tracer"):
        return functions
    from google.adk.flows.llm_flows.tools import _batch_executor

    return _batch_executor


def _adk_version() -> Tuple[int, int, int]:
    """Installed google-adk version as a (major, minor, patch) tuple."""
    from google.adk import __version__

    return cast(Tuple[int, int, int], tuple(int(x) for x in __version__.split(".")[:3]))


def _resolve_trace_tool_call_module() -> Any:
    """Module that owns ``trace_tool_call`` for the installed ADK.

    1.32+ exposes it on ``telemetry.tracing``; older ADK on
    ``flows.llm_flows.functions``.
    """
    if _adk_version() >= (1, 32, 0):
        from google.adk.telemetry import (  # type: ignore[attr-defined,import-not-found,unused-ignore]
            tracing as adk_tracing,  # type: ignore[attr-defined,unused-ignore]
        )

        return adk_tracing

    from google.adk.flows.llm_flows import functions

    return functions
