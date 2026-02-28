"""Instrumentor for AWS Strands Agents.

This module provides functions to configure Strands' built-in OpenTelemetry
support to send traces to TraceAI, along with optional method wrapping for
additional instrumentation.
"""

import os
from typing import Any, Dict, List, Optional

from opentelemetry import trace
from opentelemetry.trace import TracerProvider

from traceai_strands._attributes import (
    SpanAttributes,
    get_agent_attributes,
    get_tool_attributes,
    get_model_attributes,
    get_trace_attributes_from_config,
)


class StrandsInstrumentor:
    """Instrumentor for AWS Strands Agents.

    This class provides methods to configure Strands' built-in telemetry
    to use TraceAI as the backend, and optionally wrap agent methods for
    additional observability.

    Strands has native OpenTelemetry support via the StrandsTelemetry class.
    This instrumentor:
    1. Configures environment variables for OTLP export
    2. Provides helper methods to set up StrandsTelemetry with TraceAI
    3. Optionally wraps Agent methods for extended capture
    """

    _instance: Optional["StrandsInstrumentor"] = None
    _is_instrumented: bool = False

    def __new__(cls) -> "StrandsInstrumentor":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._tracer_provider: Optional[TracerProvider] = None
        self._original_methods: Dict[str, Any] = {}

    def instrument(
        self,
        tracer_provider: Optional[TracerProvider] = None,
        otlp_endpoint: Optional[str] = None,
        otlp_headers: Optional[Dict[str, str]] = None,
        enable_console_exporter: bool = False,
        enable_metrics: bool = True,
    ) -> "StrandsInstrumentor":
        """Configure Strands to send traces to TraceAI.

        This method sets up the necessary environment variables and
        optionally configures Strands' StrandsTelemetry class.

        Args:
            tracer_provider: Optional OpenTelemetry tracer provider.
            otlp_endpoint: OTLP endpoint URL. If not provided, uses
                OTEL_EXPORTER_OTLP_ENDPOINT environment variable.
            otlp_headers: Headers for OTLP export (e.g., authentication).
            enable_console_exporter: Whether to also export to console.
            enable_metrics: Whether to enable metrics collection.

        Returns:
            Self for method chaining.
        """
        if self._is_instrumented:
            return self

        self._tracer_provider = tracer_provider or trace.get_tracer_provider()

        # Set environment variables for Strands' native OTEL support
        if otlp_endpoint:
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = otlp_endpoint

        if otlp_headers:
            header_str = ",".join(f"{k}={v}" for k, v in otlp_headers.items())
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = header_str

        # Try to configure StrandsTelemetry if available
        try:
            self._configure_strands_telemetry(
                enable_console_exporter=enable_console_exporter,
                enable_metrics=enable_metrics,
            )
        except ImportError:
            # Strands not installed, environment variables are set
            pass

        self._is_instrumented = True
        return self

    def _configure_strands_telemetry(
        self,
        enable_console_exporter: bool = False,
        enable_metrics: bool = True,
    ) -> None:
        """Configure Strands' StrandsTelemetry class.

        Args:
            enable_console_exporter: Whether to enable console export.
            enable_metrics: Whether to enable metrics.
        """
        from strands.telemetry import StrandsTelemetry

        # Create telemetry instance with our tracer provider
        telemetry = StrandsTelemetry(tracer_provider=self._tracer_provider)

        # Set up exporters
        telemetry.setup_otlp_exporter()

        if enable_console_exporter:
            telemetry.setup_console_exporter()

        if enable_metrics:
            telemetry.setup_meter(
                enable_console_exporter=enable_console_exporter,
                enable_otlp_exporter=True,
            )

    def uninstrument(self) -> None:
        """Remove instrumentation.

        This clears the environment variables set for OTEL export.
        Note that existing Strands agents may still have telemetry
        configured.
        """
        if not self._is_instrumented:
            return

        # Clear environment variables (be careful not to remove user-set values)
        # Only clear if we set them
        self._is_instrumented = False
        self._tracer_provider = None

    @property
    def is_instrumented(self) -> bool:
        """Check if instrumentation is active."""
        return self._is_instrumented


def configure_strands_tracing(
    tracer_provider: Optional[TracerProvider] = None,
    otlp_endpoint: Optional[str] = None,
    otlp_headers: Optional[Dict[str, str]] = None,
    project_name: Optional[str] = None,
    enable_console_exporter: bool = False,
    enable_metrics: bool = True,
) -> StrandsInstrumentor:
    """Configure Strands Agents to send traces to TraceAI.

    This is the main entry point for integrating TraceAI with Strands.
    It configures Strands' built-in OpenTelemetry support to use TraceAI
    as the backend.

    Example usage:

        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType
        from traceai_strands import configure_strands_tracing

        # Option 1: Use with fi_instrumentation
        trace_provider = register(
            project_type=ProjectType.OBSERVE,
            project_name="strands-agent",
        )
        configure_strands_tracing(tracer_provider=trace_provider)

        # Option 2: Configure directly with endpoint
        configure_strands_tracing(
            otlp_endpoint="https://api.traceai.com/v1/traces",
            otlp_headers={"Authorization": "Bearer YOUR_API_KEY"},
            project_name="strands-agent",
        )

        # Now create and use Strands agents normally
        from strands import Agent
        agent = Agent(
            model="us.anthropic.claude-sonnet-4-20250514-v1:0",
            system_prompt="You are a helpful assistant."
        )
        response = agent("Hello!")

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider. If not
            provided, uses the global tracer provider.
        otlp_endpoint: OTLP endpoint URL for trace export. If not provided,
            uses the OTEL_EXPORTER_OTLP_ENDPOINT environment variable.
        otlp_headers: Headers for OTLP export (e.g., authentication).
        project_name: Optional project name for trace attributes.
        enable_console_exporter: Whether to also export traces to console.
        enable_metrics: Whether to enable metrics collection.

    Returns:
        Configured StrandsInstrumentor instance.
    """
    instrumentor = StrandsInstrumentor()

    # Add project name to headers if provided
    if project_name and otlp_headers is None:
        otlp_headers = {}
    if project_name and otlp_headers is not None:
        otlp_headers["x-project-name"] = project_name

    return instrumentor.instrument(
        tracer_provider=tracer_provider,
        otlp_endpoint=otlp_endpoint,
        otlp_headers=otlp_headers,
        enable_console_exporter=enable_console_exporter,
        enable_metrics=enable_metrics,
    )


def create_traced_agent(
    model: Any,
    system_prompt: str = "",
    tools: Optional[List[Any]] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    **kwargs: Any,
) -> Any:
    """Create a Strands Agent with tracing attributes pre-configured.

    This is a convenience function that creates a Strands Agent with
    trace_attributes set up for TraceAI integration.

    Args:
        model: The model to use (string or model instance).
        system_prompt: The agent's system prompt.
        tools: Optional list of tools for the agent.
        session_id: Optional session identifier for trace correlation.
        user_id: Optional user identifier.
        tags: Optional list of tags for filtering traces.
        **kwargs: Additional arguments passed to Agent().

    Returns:
        Configured Strands Agent instance.

    Raises:
        ImportError: If strands-agents is not installed.
    """
    try:
        from strands import Agent
    except ImportError as e:
        raise ImportError(
            "strands-agents is not installed. Install it with: "
            "pip install 'strands-agents[otel]'"
        ) from e

    # Build trace attributes
    trace_attributes = get_trace_attributes_from_config(
        session_id=session_id,
        user_id=user_id,
        tags=tags,
    )

    # Merge with any existing trace_attributes in kwargs
    if "trace_attributes" in kwargs:
        trace_attributes.update(kwargs.pop("trace_attributes"))

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools or [],
        trace_attributes=trace_attributes,
        **kwargs,
    )
