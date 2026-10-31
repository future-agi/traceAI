"""Bind Guardrails' native OpenTelemetry spans to a privacy-aware provider."""

import contextvars
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Collection

from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from traceai_guardrails._legacy import _Contextvars
from traceai_guardrails.version import __version__


class GuardrailsInstrumentor(BaseInstrumentor):
    """Use native spans where available, preserving the pre-native SDK adapter.

    Configure this once before running Guards. The supplied provider need not be
    global; unrelated tracers and the application's provider are never modified.
    Guardrails' own disable_tracing setting is respected.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("guardrails-ai>=0.5.0",)

    def _instrument(self, **kwargs: Any) -> None:
        self._bindings = []
        self._legacy = None
        try:
            if find_spec("guardrails.telemetry.guard_tracing") is None:
                from traceai_guardrails._legacy import GuardrailsInstrumentor as Legacy

                self._legacy = Legacy()
                self._legacy._instrument(**kwargs)
                return

            from traceai_guardrails._native import (
                NativeTraceAPI,
                add_call_attributes,
                add_step_attributes,
            )

            config = kwargs.get("config")
            if config is None:
                config = TraceConfig()
            if not isinstance(config, TraceConfig):
                raise TypeError("config must be a TraceConfig")
            provider = kwargs.get("tracer_provider")
            if provider is None:
                provider = trace.get_tracer_provider()
            api = NativeTraceAPI(provider, config)
            for name in ("guard_tracing", "runner_tracing", "validator_tracing"):
                module = import_module(f"guardrails.telemetry.{name}")
                self._bind(module, "trace", api)
                # Streaming guard spans use the separately imported function.
                if hasattr(module, "get_tracer"):
                    self._bind(module, "get_tracer", api.get_tracer)
                if name == "runner_tracing":
                    # Native serializers traverse mixed history and can drain an
                    # async LLMResponse iterator. Replace attribute extraction,
                    # never execution methods or the framework's span lifecycle.
                    self._bind(module, "add_call_attributes", add_call_attributes)
                    self._bind(module, "add_step_attributes", add_step_attributes)
        except Exception:
            self._uninstrument()
            raise

    def _bind(self, module: Any, name: str, replacement: Any) -> None:
        self._bindings.append((module, name, getattr(module, name), replacement))
        setattr(module, name, replacement)

    def _uninstrument(self, **kwargs: Any) -> None:
        if getattr(self, "_legacy", None) is not None:
            self._legacy._uninstrument()
            self._legacy = None
        for module, name, original, replacement in reversed(
            getattr(self, "_bindings", [])
        ):
            if getattr(module, name) is replacement:
                setattr(module, name, original)
        self._bindings = []
