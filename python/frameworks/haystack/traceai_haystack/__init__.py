"""Future AGI instrumentation through Haystack's public Tracer protocol."""

from dataclasses import replace
from typing import Any, Collection

import haystack
from fi_instrumentation import FITracer, TraceConfig
from haystack import tracing
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from traceai_haystack._bindings import bind_pipeline
from traceai_haystack._tracer import HaystackTracer
from traceai_haystack.version import __version__

_instruments = ("haystack-ai >= 2.10.0, < 4",)


class HaystackInstrumentor(BaseInstrumentor):
    """Install one native Haystack span producer.

    ``instrument(tracer_provider=..., config=...)`` remains supported. Content
    capture follows Haystack's opt-in setting unless ``capture_content`` is
    supplied explicitly. TraceConfig privacy controls always take precedence.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        capture_content = kwargs.get(
            "capture_content", tracing.tracer.is_content_tracing_enabled
        )
        if not isinstance(capture_content, bool):
            raise TypeError("capture_content must be a bool")
        if not capture_content:
            config = replace(
                config,
                hide_inputs=True,
                hide_outputs=True,
                hide_embedding_vectors=True,
                _hide_error_details=True,
            )
        provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, provider), config=config
        )
        self._adapter = HaystackTracer(self._tracer, config)
        self._previous_tracer = tracing.tracer.actual_tracer
        self._bindings = []
        # These wrappers only bind instance metadata absent from native tags.
        # They do not trace, wrap components, or touch private execution hooks.
        classes = [haystack.Pipeline]
        if hasattr(haystack, "AsyncPipeline"):
            classes.append(haystack.AsyncPipeline)
        for cls in dict.fromkeys(classes):
            for name in ("run", "run_async", "run_async_generator"):
                original = cls.__dict__.get(name)
                if original is not None:
                    bound = bind_pipeline(original)
                    setattr(cls, name, bound)
                    self._bindings.append((cls, name, original, bound))
        tracing.enable_tracing(self._adapter)
        # Preserve optional guardrail instrumentation without requiring the
        # evaluation package for Haystack tracing.
        try:
            from fi.evals import Protect
        except ImportError:
            Protect = None
        if Protect is not None:
            from fi_instrumentation.instrumentation._protect_wrapper import (
                GuardrailProtectWrapper,
            )

            original = Protect.protect
            bound = FunctionWrapper(
                original, GuardrailProtectWrapper(tracer=self._tracer)
            )
            Protect.protect = bound
            self._bindings.append((Protect, "protect", original, bound))

    def _uninstrument(self, **kwargs: Any) -> None:
        if tracing.tracer.actual_tracer is self._adapter:
            tracing.enable_tracing(self._previous_tracer)
        for cls, name, original, bound in reversed(self._bindings):
            if cls.__dict__.get(name) is bound:
                setattr(cls, name, original)
        self._bindings.clear()
