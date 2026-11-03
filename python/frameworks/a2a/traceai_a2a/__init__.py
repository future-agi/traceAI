"""Native OpenTelemetry adapter for the A2A Python SDK.

Use A2AInstrumentor().instrument(tracer_provider=provider) before creating
clients. The SDK supplies spans; this adapter supplies safe metadata,
propagation and asynchronous stream ownership.
"""

from collections.abc import Collection
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from traceai_a2a._a2a_server import A2ATracingMiddleware
from traceai_a2a._native import NativeAdapter
from traceai_a2a.package import _instruments
from traceai_a2a.version import __version__

__all__ = ["A2AInstrumentor", "A2ATracingMiddleware", "__version__"]


class A2AInstrumentor(BaseInstrumentor):
    """Configure native A2A SDK spans without changing the global provider.

    Supports the SDK's JSON-RPC, REST and installed gRPC transports, plus
    the A2AClient entry point in older SDKs. Message bodies, credentials,
    URL paths/queries and exception details are not captured.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        adapter = NativeAdapter(
            kwargs.get("tracer_provider") or trace.get_tracer_provider()
        )
        adapter.install()
        self._adapter = adapter

    def _uninstrument(self, **kwargs: Any) -> None:
        self._adapter.uninstall()
