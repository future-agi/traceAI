"""Independent sampling for agent-created spans, without changing an app sampler.

Only tracers supplied to agent integrations (including FITracer) use this adapter.
Application tracers, recording decisions and propagation flags are left untouched.
A FI-only span is RECORD_ONLY in the application context; only its FI export copy
has the sampled bit set. Application processors never see such a span.
"""

from contextvars import ContextVar
from copy import copy
from typing import Any
from weakref import WeakValueDictionary

import wrapt
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, Tracer
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult


_decisions: ContextVar = ContextVar("traceai_agent_sampling")


def route_sample(route: Any, parent_context: Any, trace_id: int, name: str,
                 kind: Any, attributes: Any, links: Any) -> bool:
    parent = trace.get_current_span(parent_context)
    if getattr(parent, "_traceai_route_owner", None) is route:
        return getattr(parent, "_traceai_route_sampled", True)
    return route.sampler.should_sample(
        parent_context, trace_id, name, kind, attributes, links).decision.is_sampled()


class _AgentSampler(Sampler):
    def __init__(self, app: Sampler, route: Any) -> None:
        self.app, self.route = app, route

    def should_sample(self, parent_context: Any, trace_id: int, name: str,
                      kind: Any = None, attributes: Any = None, links: Any = None,
                      trace_state: Any = None) -> SamplingResult:
        app = self.app.should_sample(parent_context, trace_id, name, kind, attributes, links)
        fi = self.route.enabled and (app.decision.is_sampled() if self.route.sampler is None else route_sample(
            self.route, parent_context, trace_id, name, kind, attributes, links))
        _decisions.set((app.decision.is_recording(), fi))
        if not app.decision.is_recording() and fi:
            return SamplingResult(Decision.RECORD_ONLY, app.attributes, app.trace_state)
        return app

    def get_description(self) -> str:
        return self.app.get_description()


class _AgentProcessor(SpanProcessor):
    def __init__(self, active: Any, route: Any) -> None:
        self.active, self.route = active, route
        self.live: Any = WeakValueDictionary()

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        from fi_instrumentation._attribute_limits import adopt_admission_hints
        from fi_instrumentation.instrumentation._tracers import _mark_model_coverage

        adopt_admission_hints(span)  # media context survives SDK eviction (S1 at export)
        _mark_model_coverage(span, parent_context)
        app_recording, fi = _decisions.get()
        span._traceai_app_recording = app_recording
        span._traceai_route_sampled = fi
        self.live[(span.context.trace_id, span.context.span_id)] = span
        for processor in self.active._span_processors:
            if app_recording or processor is self.route:
                processor.on_start(span, parent_context=parent_context)

    def _on_ending(self, span: Any) -> None:
        for processor in self.active._span_processors:
            if getattr(span, "_traceai_app_recording", False) or processor is self.route:
                callback = getattr(processor, "_on_ending", None)
                if callback is not None:
                    callback(span)

    def on_end(self, span: Any) -> None:
        live = self.live.pop((span.context.trace_id, span.context.span_id), None)
        # These are agent-created library/wire spans, not unrelated application
        # telemetry. Protect every destination, including earlier app processors.
        from fi_instrumentation.auto.wire._privacy import export_span

        try:
            span = export_span(span, self.route.config)
        except Exception:
            return  # Fail closed for telemetry, never for the application.
        for processor in self.active._span_processors:
            if getattr(live, "_traceai_app_recording", False) or processor is self.route:
                processor.on_end(span)


def agent_tracer(tracer: Any) -> Any:
    """Clone the agent's SDK tracer, never mutate a cached application tracer."""
    if not isinstance(tracer, Tracer) or isinstance(tracer.sampler, _AgentSampler):
        return tracer
    active = tracer.span_processor
    route = next((p for p in getattr(active, "_span_processors", ())
                  if getattr(p, "_traceai_route", False)), None)
    if route is None:
        return tracer
    cloned = copy(tracer)
    cloned.sampler = _AgentSampler(tracer.sampler, route)
    cloned.span_processor = _AgentProcessor(active, route)
    return cloned


class AgentProvider(wrapt.ObjectProxy):
    def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
        return agent_tracer(self.__wrapped__.get_tracer(*args, **kwargs))


def agent_provider(provider: Any) -> Any:
    active = getattr(provider, "_active_span_processor", None)
    if any(getattr(p, "_traceai_route", False)
           for p in getattr(active, "_span_processors", ())):
        return AgentProvider(provider)
    return provider
