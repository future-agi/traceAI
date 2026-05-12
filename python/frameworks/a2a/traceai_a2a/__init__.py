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
from typing import Any, Collection, List, Optional, Tuple

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import TracerProvider

from traceai_a2a._a2a_client import A2AClientWrapper
from traceai_a2a._a2a_server import A2ATracingMiddleware
from traceai_a2a.package import _instruments
from traceai_a2a.version import __version__

logger = logging.getLogger(__name__)

__all__ = [
    "A2AInstrumentor",
    "A2ATracingMiddleware",
    "__version__",
]

# a2a-sdk 0.x client: `A2AClient.send_task` / `A2AClient.send_task_streaming`.
_SEND_TASK_METHOD = "send_task"
_SEND_TASK_STREAMING_METHOD = "send_task_streaming"

# a2a-sdk 1.x client: `Client.send_message` (always async, always streaming —
# returns AsyncIterator[StreamResponse]). The streaming/non-streaming
# distinction collapsed into a single method.
_SEND_MESSAGE_METHOD = "send_message"

# Tag attached to the resolved target tuple so the wrapper knows whether to
# extract payload as a dict (v0) or a protobuf SendMessageRequest (v1).
_API_V0 = "v0"
_API_V1 = "v1"


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
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider: Optional[TracerProvider] = kwargs.get("tracer_provider")
        if tracer_provider is None:
            tracer_provider = trace_api.get_tracer_provider()

        tracer = tracer_provider.get_tracer(
            instrumenting_module_name="traceai_a2a",
            instrumenting_library_version=__version__,
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

        # Allow tests to pass a pre-resolved client_class directly. When a
        # tester supplies one we default to the v0 method set; if they need
        # v1 they can pass `_api_version` too.
        client_class = kwargs.get("_client_class")
        api_version: Optional[str] = kwargs.get("_api_version")

        if client_class is None:
            a2a_module = self._get_a2a_module()
            if a2a_module is None:
                logger.warning(
                    "traceai-a2a: a2a-sdk is not installed. "
                    "Install it with: pip install 'traceAI-a2a[a2a]' or pip install a2a-sdk. "
                    "A2AInstrumentor will have no effect until the SDK is installed."
                )
                return

            resolved = self._resolve_client_class(a2a_module)
            if resolved is None:
                logger.warning(
                    "traceai-a2a: Could not locate an A2A client class in the "
                    "installed a2a-sdk. The SDK structure may have changed. "
                    "Please file an issue."
                )
                return
            client_class, api_version = resolved
        elif api_version is None:
            api_version = _API_V0

        wrapper = A2AClientWrapper(tracer=tracer, api_version=api_version)

        for method_name, is_streaming in self._methods_for_api(api_version):
            if not hasattr(client_class, method_name):
                continue
            original = getattr(client_class, method_name)
            # Marker the wrapper reads to pick streaming vs non-streaming
            # handling. Safe to attach to a function object; for the v1 entry
            # point it's always True so the marker is redundant but kept
            # uniform with the v0 path.
            try:
                original._a2a_streaming = is_streaming
            except (AttributeError, TypeError):
                pass
            wrapt.wrap_function_wrapper(client_class, method_name, wrapper)
            logger.debug(
                "traceai-a2a: Patched %s.%s",
                client_class.__name__,
                method_name,
            )

        logger.info(
            "traceai-a2a v%s: A2AInstrumentor active — "
            "distributed trace context will propagate across agent boundaries.",
            __version__,
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        a2a_module = self._get_a2a_module()
        if a2a_module is None:
            return

        resolved = self._resolve_client_class(a2a_module)
        if resolved is None:
            return
        client_class, api_version = resolved

        for method_name, _ in self._methods_for_api(api_version):
            patched = getattr(client_class, method_name, None)
            if patched and hasattr(patched, "__wrapped__"):
                setattr(client_class, method_name, patched.__wrapped__)
                logger.debug(
                    "traceai-a2a: Unpatched %s.%s",
                    client_class.__name__,
                    method_name,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _methods_for_api(api_version: str) -> List[Tuple[str, bool]]:
        """Return the (method_name, is_streaming) pairs to patch per API."""
        if api_version == _API_V1:
            # `Client.send_message` always returns an AsyncIterator.
            return [(_SEND_MESSAGE_METHOD, True)]
        return [
            (_SEND_TASK_METHOD, False),
            (_SEND_TASK_STREAMING_METHOD, True),
        ]

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

    def _resolve_client_class(
        self, a2a_module: Any
    ) -> Optional[Tuple[type, str]]:
        """
        Locate the A2A client class and report which API generation it is.

        Returns (class, api_version) where api_version is "v0" for the old
        ``A2AClient.send_task`` shape and "v1" for the post-rename
        ``Client.send_message`` shape. The v1 lookup is tried first because
        installs of a2a-sdk >= 1.0 re-export the new ``Client`` class while
        no longer providing ``A2AClient``.
        """
        # a2a-sdk >= 1.0: a2a.client.Client (send_message / async streaming)
        try:
            from a2a.client import Client as _Client  # type: ignore[attr-defined]

            if isinstance(_Client, type):
                return _Client, _API_V1
        except ImportError:
            pass

        # a2a-sdk >= 0.2: a2a.client.A2AClient
        try:
            from a2a.client import A2AClient  # type: ignore[attr-defined]

            return A2AClient, _API_V0
        except ImportError:
            pass

        # Older flat-namespace fallback
        client_class = getattr(a2a_module, "A2AClient", None)
        if isinstance(client_class, type):
            return client_class, _API_V0

        return None
