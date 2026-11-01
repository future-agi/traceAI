"""Metadata-only Lambda invocation tracing using the existing OpenTelemetry SDK.

Create one :class:`LambdaTracer` at module scope and decorate the synchronous
Lambda entry point with ``tracing.wrap``. Do not also enable another Lambda
instrumentor. Model/client instrumentors can share the same tracer provider.
"""

import functools
import inspect
import logging
import math
import threading
import time
from typing import Any, Callable, TypeVar

from opentelemetry import context as context_api, trace
from opentelemetry.context import Context, _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, Status, StatusCode, TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

__all__ = ["LambdaTracer"]

logger = logging.getLogger(__name__)
_Result = TypeVar("_Result")
_propagator = TraceContextTextMapPropagator()


def _is_http(event: Any) -> bool:
    return isinstance(event, dict) and (
        event.get("version") == "2.0"
        or isinstance(event.get("httpMethod"), str)
        or (
            isinstance(event.get("requestContext"), dict)
            and "elb" in event["requestContext"]
        )
    )


def _parent(event: Any) -> Context:
    # Import only W3C traceparent, never baggage/tracestate or arbitrary event
    # contents. API Gateway v1 and ALB may use mixed-case or multi-value headers.
    carrier = {}
    if _is_http(event):
        for field in ("headers", "multiValueHeaders"):
            headers = event.get(field)
            if not isinstance(headers, dict):
                continue
            for name, value in headers.items():
                if not isinstance(name, str) or name.lower() != "traceparent":
                    continue
                if field == "multiValueHeaders":
                    value = (
                        value[0]
                        if isinstance(value, list) and len(value) == 1
                        else None
                    )
                # A malformed overriding multi-value header must not revive the
                # single-value header. Let the standard propagator reject it.
                carrier["traceparent"] = value if isinstance(value, str) else ""
    return _propagator.extract(carrier, context=Context())


class LambdaTracer:
    """Trace and flush each invocation without recording application content.

    ``tracer_provider`` is application-owned: build it once, use a batch span
    processor with a finite exporter timeout, and share it with child tracers.
    This class neither replaces the global provider nor shuts it down.

    ``flush_timeout_millis`` bounds waiting after the root span ends (default
    2000 ms), further capped by Lambda's remaining time minus ``reserve_millis``
    (default 250 ms). An expired budget skips flushing. If the remaining-time
    callback fails, flushing is skipped rather than guessing a safe deadline.

    Python cannot cancel an arbitrary provider/exporter. A timed-out flush can
    continue after return; one in-flight flush is allowed per LambdaTracer, so
    subsequent invocations cannot accumulate flush workers. They skip flushing
    while that worker is running. This is best-effort delivery, not a guarantee.
    The SDK's own background worker and shutdown behavior remain unchanged.
    """

    def __init__(
        self,
        tracer_provider: TracerProvider,
        *,
        flush_timeout_millis: int = 2000,
        reserve_millis: int = 250,
    ) -> None:
        for name, value in (
            ("flush_timeout_millis", flush_timeout_millis),
            ("reserve_millis", reserve_millis),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        self._provider = tracer_provider
        self._tracer = tracer_provider.get_tracer(__name__)
        self._flush_timeout_millis = flush_timeout_millis
        self._reserve_millis = reserve_millis
        self._cold = True
        self._cold_lock = threading.Lock()
        self._flush_lock = threading.Lock()

    def wrap(
        self, handler: Callable[[Any, Any], _Result]
    ) -> Callable[[Any, Any], _Result]:
        """Decorate a normal synchronous AWS handler; preserve its result/errors.

        Re-wrapping a handler already wrapped by this instance is idempotent.
        A different instance is rejected to avoid duplicate invocation spans.
        Async/generator handlers are not supported by the Python Lambda runtime.
        """
        owner = getattr(handler, "__fi_lambda_tracer__", None)
        if owner is self:
            return handler
        if owner is not None:
            raise ValueError("handler is already wrapped by another LambdaTracer")
        if (
            inspect.iscoroutinefunction(handler)
            or inspect.isgeneratorfunction(handler)
            or inspect.isasyncgenfunction(handler)
        ):
            raise TypeError("LambdaTracer requires a synchronous Lambda handler")

        @functools.wraps(handler)
        def wrapped(event: Any, context: Any) -> _Result:
            with self._cold_lock:
                cold, self._cold = self._cold, False
            if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return handler(event, context)
            span = trace.INVALID_SPAN
            token = context_api.attach(_parent(event))
            try:
                try:
                    attributes = {
                        "fi.span.kind": "CHAIN",
                        "faas.coldstart": cold,
                        "faas.trigger": "http" if _is_http(event) else "other",
                    }
                    request_id = getattr(context, "aws_request_id", None)
                    if isinstance(request_id, str):
                        attributes["faas.invocation_id"] = request_id
                    span = self._tracer.start_span(
                        "lambda.handler",
                        kind=SpanKind.SERVER,
                        attributes=attributes,
                        record_exception=False,
                        set_status_on_exception=False,
                    )
                except Exception:
                    logger.warning("Lambda invocation span could not be started")
                with trace.use_span(
                    span, record_exception=False, set_status_on_exception=False
                ):
                    try:
                        return handler(event, context)
                    except BaseException as error:
                        # No exception event, stack, message, body, URL or headers.
                        try:
                            span.set_attribute("error.type", type(error).__name__)
                            span.set_status(Status(StatusCode.ERROR))
                        except Exception:
                            logger.warning(
                                "Lambda invocation error status could not be set"
                            )
                        raise
            finally:
                try:
                    span.end()
                except Exception:
                    logger.warning("Lambda invocation span could not be ended")
                context_api.detach(token)
                self.flush(context)

        wrapped.__fi_lambda_tracer__ = self
        return wrapped

    def flush(self, context: Any = None) -> bool:
        """Wait within the invocation budget; never claim to cancel an exporter.

        ``True`` means the provider reported a completed flush. ``False`` means
        skipped, timed out, or failed; queued spans may still export later.
        Diagnostics deliberately exclude exporter exception details.
        """
        acquired = False
        try:
            budget = self._flush_timeout_millis / 1000
            remaining = getattr(context, "get_remaining_time_in_millis", None)
            if remaining is not None:
                remaining_millis = remaining()
                if not isinstance(remaining_millis, (int, float)) or not math.isfinite(
                    remaining_millis
                ):
                    return False
                budget = min(budget, (remaining_millis - self._reserve_millis) / 1000)
            if budget <= 0:
                return False
            force_flush = getattr(self._provider, "force_flush", None)
            if force_flush is None:
                return True  # e.g. NoOpTracerProvider when credentials are absent
            deadline = time.monotonic() + budget
            acquired = self._flush_lock.acquire(blocking=False)
            if not acquired:
                logger.warning(
                    "Lambda flush skipped: an earlier flush is still running"
                )
                return False
            completed = threading.Event()
            result = []

            def run() -> None:
                try:
                    timeout = int((deadline - time.monotonic()) * 1000)
                    if timeout > 0:
                        result.append(bool(force_flush(timeout_millis=timeout)))
                except Exception:
                    logger.warning("Lambda trace flush failed")
                finally:
                    self._flush_lock.release()
                    completed.set()

            worker = threading.Thread(target=run, name="fi-lambda-flush", daemon=True)
            worker.start()
            acquired = False  # worker owns/relinquishes the lock from here
            completed.wait(max(0, deadline - time.monotonic()))
            done = completed.is_set() and bool(result and result[0])
            if not done:
                logger.warning("Lambda trace flush incomplete; spans may be lost")
            return done
        except Exception:
            logger.warning("Lambda trace flush could not be started")
            return False
        finally:
            if acquired:
                self._flush_lock.release()
