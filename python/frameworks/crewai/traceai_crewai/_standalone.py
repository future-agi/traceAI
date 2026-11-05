"""Public standalone execution; provider instrumentation owns LLM call spans."""

import asyncio
import json
from contextlib import contextmanager

from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api

from ._privacy import _safe_data
from ._wrappers import (
    _bound_call,
    _hide_input_content,
    _safe_context_attributes,
    _safe_output,
)


class _AgentKickoffWrapper:
    _kind = FiSpanKindValues.AGENT.value
    _input_field = "messages"

    def __init__(self, tracer):
        self._tracer = tracer

    def _suppressed(self, instance):
        return bool(context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY))

    @contextmanager
    def _span(self, wrapped, instance, args, kwargs):
        # Select only public application content, never Agent configuration,
        # Flow state, file objects, response schemas or checkpoint machinery.
        content = (
            "__REDACTED__"
            if _hide_input_content(self._tracer)
            else json.dumps(
                _safe_data(_bound_call(wrapped, args, kwargs).get(self._input_field))
            )
        )
        with self._tracer.start_as_current_span(
            f"{type(instance).__name__}.{wrapped.__name__}",
            attributes={
                SpanAttributes.GEN_AI_SPAN_KIND: self._kind,
                SpanAttributes.INPUT_VALUE: content,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException:
                span.set_status(trace_api.StatusCode.ERROR)
                raise

    def _finish(self, span, result):
        span.set_attribute(
            SpanAttributes.OUTPUT_VALUE, json.dumps(_safe_output(result))
        )
        span.set_attributes(_safe_context_attributes())
        span.set_status(trace_api.StatusCode.OK)

    def __call__(self, wrapped, instance, args, kwargs):
        if self._suppressed(instance):
            return wrapped(*args, **kwargs)
        # Modern Agent.kickoff is a coroutine factory inside an event loop.
        # Its kickoff_async wrapper owns the actual awaited execution, including
        # the consuming parent context and cancellation. Do not trace the factory.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            result = wrapped(*args, **kwargs)
            self._finish(span, result)
            return result


class _AsyncAgentKickoffWrapper(_AgentKickoffWrapper):
    async def __call__(self, wrapped, instance, args, kwargs):
        if self._suppressed(instance):
            return await wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            result = await wrapped(*args, **kwargs)
            self._finish(span, result)
            return result


class _AsyncFlowKickoffWrapper(_AsyncAgentKickoffWrapper):
    _kind = FiSpanKindValues.CHAIN.value
    _input_field = "inputs"

    def _suppressed(self, instance):
        return bool(
            super()._suppressed(instance)
            # AgentExecutor is itself a private Flow. Task/Agent spans already
            # own that execution; user-created nested Flows still get children.
            or getattr(type(instance), "is_crewai_internal", False)
            # Early 1.15 releases predate that marker. Match the defining
            # framework class, not a user Flow that happens to share its name.
            or any(
                cls.__module__ == "crewai.experimental.agent_executor"
                and cls.__name__ == "AgentExecutor"
                for cls in type(instance).__mro__
            )
            # Streaming factories call kickoff_async again in the consuming
            # worker with stream=False, just like Crew's lazy stream factories.
            or getattr(instance, "stream", False)
        )


class _AsyncFlowMethodWrapper(_AsyncFlowKickoffWrapper):
    """Trace the awaited 1.15+ node execution, not its listener dispatch.

    The native runtime propagates the current context into synchronous worker
    threads and awaits coroutine results. Keeping that execution untouched also
    preserves cancellation, branching and nested Agent/Flow parentage.
    """

    async def __call__(self, wrapped, instance, args, kwargs):
        if self._suppressed(instance):
            return await wrapped(*args, **kwargs)
        bound = _bound_call(wrapped, args, kwargs)
        name = bound["method_name"]
        definition = instance._definition.methods[name]
        node_type = "listen"
        if definition.router:
            node_type = "router"
        elif definition.is_start:
            node_type = "start"
        content = "__REDACTED__"
        if not _hide_input_content(self._tracer):
            # Name only the application's arguments, never the callable, Flow
            # state/definition, event IDs, persistence or executor configuration.
            inputs = _bound_call(
                bound["method"], bound.get("args", ()), bound.get("kwargs", {})
            )
            inputs.pop("self", None)
            content = json.dumps(_safe_data(inputs))
        with self._tracer.start_as_current_span(
            f"{type(instance).__name__}.{name}",
            attributes={
                SpanAttributes.GEN_AI_SPAN_KIND: self._kind,
                "flow.node.name": name,
                "flow.node.type": node_type,
                SpanAttributes.INPUT_VALUE: content,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                result = await wrapped(*args, **kwargs)
                # The second element is CrewAI's event ID, not user output.
                self._finish(span, result[0])
                return result
            except BaseException:
                span.set_status(trace_api.StatusCode.ERROR)
                raise
