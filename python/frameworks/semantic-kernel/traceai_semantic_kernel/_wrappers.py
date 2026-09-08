import logging
from typing import Any, Callable

from fi_instrumentation import FITracer
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import Status, StatusCode

logger = logging.getLogger(__name__)


class _KernelInvokeWrapper:
    def __init__(self, tracer: FITracer):
        self._tracer = tracer

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Any,
        kwargs: Any,
    ) -> Any:
        span_name = "Kernel.invoke"
        with self._tracer.start_as_current_span(
            name=span_name,
            kind=SpanKind.INTERNAL,
        ) as span:
            try:
                span.set_attribute(SpanAttributes.FI_AGENT_NAME, "SemanticKernel")
                result = await wrapped(*args, **kwargs)
                if result:
                    span.set_attribute(SpanAttributes.FI_AGENT_OUTPUT, str(result))
                return result
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise


class _FunctionInvokeWrapper:
    def __init__(self, tracer: FITracer):
        self._tracer = tracer

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Any,
        kwargs: Any,
    ) -> Any:
        span_name = "KernelFunction.invoke"
        with self._tracer.start_as_current_span(
            name=span_name,
            kind=SpanKind.INTERNAL,
        ) as span:
            try:
                if hasattr(instance, "name"):
                    span.set_attribute(SpanAttributes.FI_TOOL_NAME, instance.name)
                result = await wrapped(*args, **kwargs)
                if result:
                    span.set_attribute(SpanAttributes.FI_TOOL_OUTPUT, str(result))
                return result
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise
