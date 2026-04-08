"""
traceai-a2a: OpenTelemetry instrumentation for Google's Agent-to-Agent (A2A) Protocol.

Enables distributed tracing across multi-agent boundaries by propagating
W3C TraceContext headers automatically on every A2A call.

Basic usage:
    from traceai_a2a import A2AInstrumentor
    A2AInstrumentor().instrument(tracer_provider=trace_provider)

For server-side (receiving agent):
    from traceai_a2a import A2ATracingMiddleware
    app.add_middleware(A2ATracingMiddleware, tracer_provider=trace_provider)
"""

import logging
from typing import Any, Collection, Optional

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import TracerProvider

from traceai_a2a._a2a_client import A2AClientWrapper
from traceai_a2a._a2a_server import A2ATracingMiddleware
from traceai_a2a.version import __version__

logger = logging.getLogger(__name__)

__all__ = [
    "A2AInstrumentor",
    "A2ATracingMiddleware",
    "__version__",
]

# These are the methods we patch on A2AClient
_SEND_TASK_METHOD = "send_task"
_SEND_TASK_STREAMING_METHOD = "send_task_streaming"


class A2AInstrumentor(BaseInstrumentor):
    """
    OpenTelemetry instrumentor for the Google Agent-to-Agent (A2A) Protocol.

    Instruments A2AClient to:
    - Create an A2A_CLIENT span for every outbound agent call
    - Inject W3C TraceContext (traceparent/tracestate) into outbound HTTP headers
    - Record task ID, task state, agent URL, message role, and artifact type
    - Stitch distributed multi-agent traces into a single trace view

    Installation:
        pip install traceAI-a2a

    Usage:
        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType
        from traceai_a2a import A2AInstrumentor

        trace_provider = register(project_type=ProjectType.OBSERVE, project_name="my_app")
        A2AInstrumentor().instrument(tracer_provider=trace_provider)

        # Now use A2AClient as normal — tracing is automatic
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        """
        Declare soft dependency on the A2A SDK.
        If a2a-sdk is not installed, A2AInstrumentor will still import cleanly
        but will log a warning when instrument() is called.
        """
        return ("a2a-sdk >= 0.2.0",)

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider: Optional[TracerProvider] = kwargs.get("tracer_provider")
        if tracer_provider is None:
            tracer_provider = trace_api.get_tracer_provider()

        tracer = tracer_provider.get_tracer(
            instrumenting_module_name="traceai_a2a",
            instrumenting_library_version=__version__,
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

        # Allow tests to pass a pre-resolved client_class directly
        client_class = kwargs.get("_client_class")

        if client_class is None:
            a2a_module = self._get_a2a_module()
            if a2a_module is None:
                logger.warning(
                    "traceai-a2a: a2a-sdk is not installed. "
                    "Install it with: pip install 'traceAI-a2a[a2a]' or pip install a2a-sdk. "
                    "A2AInstrumentor will have no effect until the SDK is installed."
                )
                return

            client_class = self._get_client_class(a2a_module)
            if client_class is None:
                logger.warning(
                    "traceai-a2a: Could not locate A2AClient class in a2a module. "
                    "The SDK structure may have changed. Please file an issue."
                )
                return

        wrapper = A2AClientWrapper(tracer=tracer)

        # Patch send_task (sync/async) — marks it as non-streaming
        if hasattr(client_class, _SEND_TASK_METHOD):
            original_send_task = getattr(client_class, _SEND_TASK_METHOD)
            original_send_task._a2a_streaming = False
            wrapt.wrap_function_wrapper(
                client_class,
                _SEND_TASK_METHOD,
                wrapper,
            )
            logger.debug("traceai-a2a: Patched A2AClient.send_task")

        # Patch send_task_streaming (sync/async) — marks it as streaming
        if hasattr(client_class, _SEND_TASK_STREAMING_METHOD):
            original_send_task_streaming = getattr(
                client_class, _SEND_TASK_STREAMING_METHOD
            )
            original_send_task_streaming._a2a_streaming = True
            wrapt.wrap_function_wrapper(
                client_class,
                _SEND_TASK_STREAMING_METHOD,
                wrapper,
            )
            logger.debug("traceai-a2a: Patched A2AClient.send_task_streaming")

        logger.info(
            "traceai-a2a v%s: A2AInstrumentor active — "
            "distributed trace context will propagate across agent boundaries.",
            __version__,
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        a2a_module = self._get_a2a_module()
        if a2a_module is None:
            return

        client_class = self._get_client_class(a2a_module)
        if client_class is None:
            return

        for method_name in (_SEND_TASK_METHOD, _SEND_TASK_STREAMING_METHOD):
            patched = getattr(client_class, method_name, None)
            if patched and hasattr(patched, "__wrapped__"):
                setattr(client_class, method_name, patched.__wrapped__)
                logger.debug("traceai-a2a: Unpatched A2AClient.%s", method_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_a2a_module(self) -> Optional[Any]:
        """
        Try to import the a2a SDK module without hard-dependency.
        Returns the module or None if not installed.
        """
        try:
            import a2a
            return a2a
        except ImportError:
            return None

    def _get_client_class(self, a2a_module: Any) -> Optional[type]:
        """
        Locate the A2AClient class in the a2a module.
        Tries common locations used across SDK versions.
        """
        # a2a-sdk >= 0.2.x: a2a.client.A2AClient
        try:
            from a2a.client import A2AClient
            return A2AClient
        except ImportError:
            pass

        # Fallback: a2a.A2AClient (flat namespace in older versions)
        client_class = getattr(a2a_module, "A2AClient", None)
        if client_class is not None and isinstance(client_class, type):
            return client_class

        return None
