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
        """Patch the LLM call tracing functionality to use our tracer.

        Prefers patching ``google.adk.telemetry.tracing`` (the source of
        truth in google-adk >= 1.x where the trace functions were
        centralized). Falls back to the older
        ``google.adk.flows.llm_flows.base_llm_flow`` re-export for old
        google-adk versions where ``telemetry.tracing`` doesn't exist.
        """
        from traceai_google_adk._wrappers import _TraceCallLlm

        target, attr = self._resolve_trace_call_llm_target()
        if target is None:
            return
        setattr(target, "tracer", self._tracer)
        setattr(
            target,
            attr,
            _TraceCallLlm(self._tracer)(getattr(target, attr)),  # type: ignore[attr-defined]
        )

    def _unpatch_trace_call_llm(self) -> None:
        """Restore the original LLM call tracing functionality."""
        target, attr = self._resolve_trace_call_llm_target()
        if target is None:
            return
        current = getattr(target, attr, None)
        if callable(original := getattr(current, "__wrapped__", None)):
            setattr(target, attr, original)

        try:
            from google.adk.telemetry import tracer  # type: ignore[attr-defined]

            setattr(target, "tracer", tracer)
        except ImportError:
            pass

    @staticmethod
    def _resolve_trace_call_llm_target() -> Tuple[Any, str]:
        """Return the (module, attr_name) to patch for trace_call_llm.

        google-adk 1.x exposes the canonical definition at
        ``google.adk.telemetry.tracing``; older versions only re-export
        it through ``google.adk.flows.llm_flows.base_llm_flow``.
        """
        try:
            from google.adk.telemetry import tracing as adk_tracing

            if hasattr(adk_tracing, "trace_call_llm"):
                return adk_tracing, "trace_call_llm"
        except ImportError:
            pass

        try:
            from google.adk.flows.llm_flows import base_llm_flow

            if hasattr(base_llm_flow, "trace_call_llm"):
                return base_llm_flow, "trace_call_llm"
        except ImportError:
            pass
        return None, "trace_call_llm"  # type: ignore[return-value]

    def _patch_trace_tool_call(self) -> None:
        """Patch the tool call tracing functionality to use our tracer.

        In google-adk 1.x the rich per-tool tracing function moved to
        ``google.adk.telemetry.tracing.trace_tool_call`` (with its
        original signature). The old patch target
        ``google.adk.flows.llm_flows.functions.trace_tool_call`` was
        renamed away, so we prefer the new location and fall back to
        the old one for compatibility with pre-1.x ADK.
        """
        from traceai_google_adk._wrappers import _TraceToolCall

        target, attr = self._resolve_trace_tool_call_target()
        if target is None:
            return
        setattr(target, "tracer", self._tracer)
        setattr(
            target,
            attr,
            _TraceToolCall(self._tracer)(getattr(target, attr)),  # type: ignore[attr-defined]
        )

    def _unpatch_trace_tool_call(self) -> None:
        """Restore the original tool call tracing functionality."""
        target, attr = self._resolve_trace_tool_call_target()
        if target is None:
            return
        current = getattr(target, attr, None)
        if callable(original := getattr(current, "__wrapped__", None)):
            setattr(target, attr, original)

        try:
            from google.adk.telemetry import tracer  # type: ignore[attr-defined]

            setattr(target, "tracer", tracer)
        except ImportError:
            pass

    @staticmethod
    def _resolve_trace_tool_call_target() -> Tuple[Any, str]:
        """Return the (module, attr_name) to patch for trace_tool_call.

        google-adk 1.x keeps the rich-signature ``trace_tool_call`` in
        ``google.adk.telemetry.tracing`` (called from
        ``record_tool_execution``). Older ADK had it re-exported from
        ``google.adk.flows.llm_flows.functions``.
        """
        try:
            from google.adk.telemetry import tracing as adk_tracing

            if hasattr(adk_tracing, "trace_tool_call"):
                return adk_tracing, "trace_tool_call"
        except ImportError:
            pass

        try:
            from google.adk.flows.llm_flows import functions

            if hasattr(functions, "trace_tool_call"):
                return functions, "trace_tool_call"
        except ImportError:
            pass
        return None, "trace_tool_call"  # type: ignore[return-value]

    def _disable_existing_tracers(self) -> None:
        """Disable existing tracers to prevent double-instrumentation."""
        from google.adk import runners

        if isinstance(getattr(runners, "tracer", None), Tracer):
            setattr(runners, "tracer", _PassthroughTracer(runners.tracer))

        # google-adk 1.x removed the module-level `tracer` from
        # base_agent. Guard so the import error doesn't kill instrument().
        try:
            from google.adk.agents import base_agent
        except ImportError:
            base_agent = None  # type: ignore[assignment]
        if base_agent is not None and isinstance(
            getattr(base_agent, "tracer", None), Tracer
        ):
            setattr(base_agent, "tracer", _PassthroughTracer(base_agent.tracer))

    def _restore_existing_tracers(self) -> None:
        """Restore original tracers that were disabled during instrumentation."""
        from google.adk import runners

        runners_tracer = getattr(runners, "tracer", None)
        if isinstance(
            original := getattr(runners_tracer, "__wrapped__", None), Tracer
        ):
            setattr(runners, "tracer", original)

        try:
            from google.adk.agents import base_agent
        except ImportError:
            base_agent = None  # type: ignore[assignment]
        if base_agent is not None:
            base_agent_tracer = getattr(base_agent, "tracer", None)
            if isinstance(
                original := getattr(base_agent_tracer, "__wrapped__", None), Tracer
            ):
                setattr(base_agent, "tracer", original)


class _PassthroughTracer(wrapt.ObjectProxy):  # type: ignore[misc]
    """A tracer proxy that passes through span operations without creating new spans.

    This is used to disable existing tracers during instrumentation to prevent
    double-instrumentation of the same operations.
    """

    @_agnosticcontextmanager
    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Iterator[Span]:
        """Return the current span without creating a new one."""
        yield get_current_span()