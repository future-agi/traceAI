"""
A2A Protocol server-side instrumentation — ASGI middleware.

This middleware extracts incoming W3C TraceContext headers (traceparent/tracestate)
from A2A HTTP requests and starts a child span in that distributed trace.

Usage with Starlette / FastAPI:
    from starlette.applications import Starlette
    from traceai_a2a import A2ATracingMiddleware

    app = Starlette()
    app.add_middleware(A2ATracingMiddleware, tracer_provider=trace_provider)

When an A2A orchestrator calls this agent, its trace_id propagates via the
traceparent header. This middleware picks it up and ensures the receiving
agent's spans appear as children in the same distributed trace.
"""

import logging
from typing import Any, Callable, Dict, Optional

from opentelemetry import propagate
from opentelemetry import trace as trace_api
from opentelemetry.trace import Status, StatusCode, Tracer, TracerProvider

from traceai_a2a._attributes import get_task_attributes
from traceai_a2a._semantic_conventions import (
    A2A_AGENT_URL,
    A2A_MESSAGE_ROLE,
    A2A_SPAN_KIND_SERVER,
    A2A_TASK_ID,
    A2A_TASK_STATE,
    TASK_STATE_COMPLETED,
)

try:
    from fi_instrumentation.fi_types import SpanAttributes
    _FI_AVAILABLE = True
except ImportError:
    _FI_AVAILABLE = False

logger = logging.getLogger(__name__)


class _ASGICarrier:
    """
    Read-only carrier that exposes ASGI scope headers as a dict-like object
    for use with OpenTelemetry's propagate.extract().

    ASGI headers are list of (bytes, bytes) tuples; we decode them on access.
    """

    def __init__(self, headers: list) -> None:
        self._headers: Dict[str, str] = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in headers
        }

    def __getitem__(self, key: str) -> str:
        return self._headers[key.lower()]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._headers

    def keys(self):
        return self._headers.keys()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._headers.get(key.lower(), default)


class A2ATracingMiddleware:
    """
    ASGI middleware that extracts W3C TraceContext from incoming A2A HTTP requests
    and creates an A2A_SERVER child span in the propagated distributed trace.

    Compatible with Starlette, FastAPI, and any ASGI framework.

    Args:
        app: The ASGI application to wrap.
        tracer_provider: The OpenTelemetry TracerProvider to use.
            If None, the global tracer provider is used.
        agent_url: Optional self-identification URL for this agent
            (recorded as gen_ai.a2a.agent.url on the span).
    """

    def __init__(
        self,
        app: Any,
        tracer_provider: Optional[TracerProvider] = None,
        agent_url: Optional[str] = None,
    ) -> None:
        self._app = app
        self._agent_url = agent_url or "self"
        tracer_provider = tracer_provider or trace_api.get_tracer_provider()
        self._tracer: Tracer = tracer_provider.get_tracer(
            "traceai_a2a.server",
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable,
        send: Callable,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # Only trace A2A task-related paths
        path: str = scope.get("path", "")
        if not self._is_a2a_path(path):
            await self._app(scope, receive, send)
            return

        # Extract W3C TraceContext from incoming request headers
        headers = scope.get("headers", [])
        carrier = _ASGICarrier(headers)
        context = propagate.extract(carrier)

        span_name = self._infer_span_name(path)
        attributes: Dict[str, Any] = {
            A2A_AGENT_URL: self._agent_url,
        }
        if _FI_AVAILABLE:
            attributes[SpanAttributes.GEN_AI_SPAN_KIND] = A2A_SPAN_KIND_SERVER

        with self._tracer.start_as_current_span(
            name=span_name,
            context=context,  # <-- this is the key: parent context from traceparent header
            attributes=attributes,
        ) as span:
            # Capture response status via a response wrapper
            response_status: Dict[str, Any] = {}

            async def send_wrapper(event: Dict[str, Any]) -> None:
                if event.get("type") == "http.response.start":
                    response_status["status_code"] = event.get("status", 200)
                await send(event)

            try:
                await self._app(scope, receive, send_wrapper)

                status_code = response_status.get("status_code", 200)
                if status_code and status_code < 400:
                    span.set_status(Status(StatusCode.OK))
                    span.set_attribute(A2A_TASK_STATE, TASK_STATE_COMPLETED)
                else:
                    span.set_status(
                        Status(StatusCode.ERROR, f"HTTP {status_code}")
                    )

            except Exception as exc:
                span.record_exception(exc)
                span.set_status(
                    Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
                )
                raise

    def _is_a2a_path(self, path: str) -> bool:
        """
        Determine if a request path corresponds to an A2A protocol endpoint.
        A2A spec defines these required paths:
          POST /  (send_task, send_task_streaming)
          GET  /.well-known/agent.json
        """
        a2a_paths = {"/", ""}
        a2a_prefixes = ("/.well-known/agent", "/a2a", "/tasks")
        return path in a2a_paths or any(path.startswith(p) for p in a2a_prefixes)

    def _infer_span_name(self, path: str) -> str:
        """Produce a clean span name from the A2A request path."""
        if path in ("/", ""):
            return "A2AServer.handle_task"
        if "agent.json" in path or ".well-known" in path:
            return "A2AServer.agent_card"
        return f"A2AServer{path}"
