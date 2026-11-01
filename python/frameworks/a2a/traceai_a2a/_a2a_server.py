"""Optional ASGI boundary for applications without HTTP server instrumentation."""

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, StatusCode, TracerProvider

from traceai_a2a._native import safe_origin
from traceai_a2a._semantic_conventions import A2A_AGENT_URL, A2A_SPAN_KIND_SERVER


class A2ATracingMiddleware:
    """Extract HTTP trace context around an A2A ASGI application.

    Wrap the A2A application itself, not an unrelated multi-route app. When an
    outer HTTP instrumentor already created a SERVER span, reuse it. HTTP
    success does not imply that an asynchronous A2A task has completed.
    """

    def __init__(
        self,
        app: Any,
        tracer_provider: TracerProvider | None = None,
        agent_url: str | None = None,
    ) -> None:
        self._app = app
        self._agent_url = safe_origin(agent_url)
        self._tracer = (tracer_provider or trace.get_tracer_provider()).get_tracer(
            "traceai_a2a.server"
        )

    async def __call__(
        self, scope: dict[str, Any], receive: Callable, send: Callable
    ) -> None:
        if scope.get("type") != "http" or context.get_value(
            _SUPPRESS_INSTRUMENTATION_KEY
        ):
            await self._app(scope, receive, send)
            return
        current = trace.get_current_span()
        if getattr(current, "kind", None) == SpanKind.SERVER:
            manager = nullcontext(current)
        else:
            carrier = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            manager = self._tracer.start_as_current_span(
                "A2AServer.request",
                kind=SpanKind.SERVER,
                context=propagate.extract(carrier),
                record_exception=False,
                set_status_on_exception=False,
            )
        with manager as span:
            span.set_attribute("gen_ai.span.kind", A2A_SPAN_KIND_SERVER)
            if self._agent_url:
                span.set_attribute(A2A_AGENT_URL, self._agent_url)

            async def send_response(event):
                if event.get("type") == "http.response.start":
                    status = event["status"]
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(StatusCode.ERROR)
                await send(event)

            try:
                await self._app(scope, receive, send_response)
            except asyncio.CancelledError:
                span.set_attribute("gen_ai.a2a.cancelled", True)
                raise
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(StatusCode.ERROR)
                raise
