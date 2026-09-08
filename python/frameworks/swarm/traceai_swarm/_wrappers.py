import logging
from typing import Any, Callable

from fi_instrumentation import FITracer
from fi_instrumentation.events import Events
from opentelemetry.trace import SpanKind

logger = logging.getLogger(__name__)


class _SwarmRunWrapper:
    def __init__(self, tracer: FITracer):
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Any,
        kwargs: Any,
    ) -> Any:
        span_name = "Swarm.run"
        with self._tracer.start_as_current_span(
            name=span_name,
            kind=SpanKind.INTERNAL,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_attribute(Events.AGENT_NAME, "Swarm")
                if result and hasattr(result, "messages"):
                    # Record the final messages
                    self._tracer.record_event(
                        span, Events.AGENT_RUN_SUCCESS, {"messages": result.messages}
                    )
                return result
            except Exception as e:
                self._tracer.record_exception(span, e)
                raise
