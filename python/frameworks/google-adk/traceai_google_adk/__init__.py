"""Google ADK's native OpenTelemetry spans, enriched for Future AGI."""

import sys
from types import ModuleType
from typing import Any, Collection

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore[attr-defined]

from fi_instrumentation import TraceConfig
from traceai_google_adk._native import NativeTracer
from traceai_google_adk.version import __version__


def _bindings(tracer: Any) -> list[tuple[ModuleType, str]]:
    """Find already-imported aliases of ADK's shared tracer, by identity.

    New ADK modules import the adapted tracer from telemetry; no import hook or
    version-dependent list of execution internals is needed.
    """
    return [
        (module, key)
        for name, module in tuple(sys.modules.items())
        if name.startswith("google.adk.") and isinstance(module, ModuleType)
        for key, value in tuple(vars(module).items())
        if value is tracer
    ]


class GoogleADKInstrumentor(BaseInstrumentor):
    """Route native ADK spans to an OTel provider with FI mapping and privacy.

    Configure once at application startup, before running agents. This does not
    wrap Runner, agents, model calls, or tools, or configure logs/metrics. Do not
    combine with another ADK instrumentor. Omitted config hides content; pass an
    explicit TraceConfig to select a different capture policy.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("google-adk >= 1.2.1",)

    def _instrument(self, **kwargs: Any) -> None:
        from google.adk import telemetry
        from google.adk.agents import BaseAgent, base_agent
        from google.adk.runners import Runner

        if any(
            hasattr(method, "__wrapped__")
            for method in (Runner.run_async, BaseAgent.run_async)
        ):
            raise RuntimeError(
                "Remove other Google ADK instrumentors before enabling traceAI"
            )
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig(hide_inputs=True, hide_outputs=True)
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        sources = [telemetry.tracer]
        # Older ADK creates a separate agent tracer instead of importing the
        # shared telemetry tracer. Preserve each original object independently.
        agent_tracer = vars(base_agent).get("tracer")
        if agent_tracer is not None and all(agent_tracer is not t for t in sources):
            sources.append(agent_tracer)
        provider = kwargs.get("tracer_provider") or trace.get_tracer_provider()
        plans = []
        for original in sources:
            if not isinstance(original, trace.Tracer):
                raise RuntimeError(
                    "Google ADK does not expose its native OpenTelemetry tracer"
                )
            adapter = NativeTracer(provider.get_tracer(__name__, __version__), config)
            bindings = _bindings(original)
            if not bindings:
                raise RuntimeError("Google ADK native tracer bindings were not found")
            plans.append((original, adapter, bindings))
        changed = []
        try:
            for original, adapter, bindings in plans:
                for module, name in bindings:
                    changed.append((module, name, original, adapter))
                    setattr(module, name, adapter)
        except BaseException:
            for module, name, original, adapter in reversed(changed):
                if vars(module).get(name) is adapter:
                    setattr(module, name, original)
            raise
        self._tracers = [(original, adapter) for original, adapter, _ in plans]

    def _uninstrument(self, **kwargs: Any) -> None:
        # Include aliases imported after setup. Leave another owner's later
        # replacement alone, and never shut down the caller's provider.
        for original, adapter in self._tracers:
            for module, name in _bindings(adapter):
                setattr(module, name, original)
