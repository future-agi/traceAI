"""Instrumentor for Agno Agent Framework.

This module provides functions to configure Agno's OpenInference-based
instrumentation to send traces to TraceAI.
"""

import os
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.trace import TracerProvider
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
from opentelemetry.sdk.resources import Resource


class AgnoInstrumentorWrapper:
    """Wrapper around Agno's OpenInference instrumentor.

    This class provides a convenient way to configure Agno's built-in
    OpenInference instrumentation to use TraceAI as the backend.

    Important: Call instrument() BEFORE creating any Agno agents.
    """

    _instance: Optional["AgnoInstrumentorWrapper"] = None
    _is_instrumented: bool = False

    def __new__(cls) -> "AgnoInstrumentorWrapper":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._tracer_provider: Optional[TracerProvider] = None
        self._agno_instrumentor: Any = None

    def instrument(
        self,
        tracer_provider: Optional[TracerProvider] = None,
        otlp_endpoint: Optional[str] = None,
        otlp_headers: Optional[Dict[str, str]] = None,
    ) -> "AgnoInstrumentorWrapper":
        """Configure Agno to send traces to TraceAI.

        This method sets up OpenTelemetry and the Agno instrumentor.
        Must be called BEFORE creating any Agno agents.

        Args:
            tracer_provider: Optional OpenTelemetry tracer provider.
            otlp_endpoint: OTLP endpoint URL.
            otlp_headers: Headers for OTLP export (e.g., authentication).

        Returns:
            Self for method chaining.
        """
        if self._is_instrumented:
            return self

        # Set environment variables for OTLP export
        if otlp_endpoint:
            os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = otlp_endpoint

        if otlp_headers:
            header_str = ",".join(f"{k}={v}" for k, v in otlp_headers.items())
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = header_str

        # Set up tracer provider
        if tracer_provider:
            self._tracer_provider = tracer_provider
            trace.set_tracer_provider(tracer_provider)
        else:
            self._tracer_provider = trace.get_tracer_provider()

        # Configure OpenInference Agno instrumentor
        try:
            self._setup_agno_instrumentor()
        except ImportError:
            # openinference-instrumentation-agno not installed
            pass

        self._is_instrumented = True
        return self

    def _setup_agno_instrumentor(self) -> None:
        """Set up the Agno OpenInference instrumentor."""
        try:
            from openinference.instrumentation.agno import AgnoInstrumentor

            self._agno_instrumentor = AgnoInstrumentor()
            self._agno_instrumentor.instrument()
        except ImportError:
            raise ImportError(
                "openinference-instrumentation-agno is not installed. "
                "Install it with: pip install openinference-instrumentation-agno"
            )

    def uninstrument(self) -> None:
        """Remove instrumentation."""
        if not self._is_instrumented:
            return

        if self._agno_instrumentor:
            try:
                self._agno_instrumentor.uninstrument()
            except Exception:
                pass

        self._is_instrumented = False
        self._tracer_provider = None

    @property
    def is_instrumented(self) -> bool:
        """Check if instrumentation is active."""
        return self._is_instrumented


def configure_agno_tracing(
    tracer_provider: Optional[TracerProvider] = None,
    otlp_endpoint: Optional[str] = None,
    otlp_headers: Optional[Dict[str, str]] = None,
    project_name: Optional[str] = None,
) -> AgnoInstrumentorWrapper:
    """Configure Agno Agent Framework to send traces to TraceAI.

    This is the main entry point for integrating TraceAI with Agno.
    Must be called BEFORE creating any Agno agents.

    Example usage:

        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType
        from traceai_agno import configure_agno_tracing

        # Option 1: Use with fi_instrumentation
        trace_provider = register(
            project_type=ProjectType.OBSERVE,
            project_name="agno-agent",
        )
        configure_agno_tracing(tracer_provider=trace_provider)

        # Option 2: Configure directly with endpoint
        configure_agno_tracing(
            otlp_endpoint="https://api.traceai.com/v1/traces",
            otlp_headers={"Authorization": "Bearer YOUR_API_KEY"},
            project_name="agno-agent",
        )

        # IMPORTANT: Now import Agno modules
        from agno.agent import Agent
        from agno.models.openai import OpenAIChat

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider.
        otlp_endpoint: OTLP endpoint URL for trace export.
        otlp_headers: Headers for OTLP export (e.g., authentication).
        project_name: Optional project name for trace attributes.

    Returns:
        Configured AgnoInstrumentorWrapper instance.
    """
    wrapper = AgnoInstrumentorWrapper()

    # Add project name to headers if provided
    if project_name and otlp_headers is None:
        otlp_headers = {}
    if project_name and otlp_headers is not None:
        otlp_headers["x-project-name"] = project_name

    return wrapper.instrument(
        tracer_provider=tracer_provider,
        otlp_endpoint=otlp_endpoint,
        otlp_headers=otlp_headers,
    )


def setup_traceai_exporter(
    endpoint: str,
    headers: Optional[Dict[str, str]] = None,
    use_batch_processor: bool = True,
    service_name: str = "agno-agent",
) -> TracerProvider:
    """Set up a tracer provider with OTLP exporter for TraceAI.

    This is a convenience function that creates a complete tracer provider
    configured to export to TraceAI.

    Args:
        endpoint: OTLP endpoint URL.
        headers: Optional headers for authentication.
        use_batch_processor: Whether to use batch processing (recommended).
        service_name: Service name for the traces.

    Returns:
        Configured TracerProvider.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        raise ImportError(
            "opentelemetry-exporter-otlp is not installed. "
            "Install it with: pip install opentelemetry-exporter-otlp"
        )

    # Create resource
    resource = Resource.create({"service.name": service_name})

    # Create tracer provider
    provider = SDKTracerProvider(resource=resource)

    # Create exporter
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers or {},
    )

    # Add span processor
    if use_batch_processor:
        processor = BatchSpanProcessor(exporter)
    else:
        processor = SimpleSpanProcessor(exporter)

    provider.add_span_processor(processor)

    # Set as global
    trace.set_tracer_provider(provider)

    return provider
