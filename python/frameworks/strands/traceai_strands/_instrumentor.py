"""Instrumentor for AWS Strands Agents.

This module provides functions to configure Strands' built-in OpenTelemetry
support to send traces to TraceAI, along with optional method wrapping for
additional instrumentation.
"""

import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.trace import TracerProvider

from traceai_strands._attributes import (
    SpanAttributes,
    get_agent_attributes,
    get_tool_attributes,
    get_model_attributes,
    get_trace_attributes_from_config,
)


logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Ways the upstream OTLP/HTTP span exporter authenticates outside its headers: a client
# certificate (mTLS) or a credential provider that builds its requests session.
_CREDENTIALS_OUTSIDE_HEADERS = (
    "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
    "OTEL_EXPORTER_OTLP_CLIENT_KEY",
    "_OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
    "_OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
)


def _traces_url(url: Any) -> Optional[Tuple[str, str, str, int, str, str]]:
    """An http(s) URL normalised for comparison, or None for anything else.

    (scheme, userinfo, host, port, path, query): scheme and host lower-cased, the default port
    filled in, repeated and trailing slashes in the path dropped, the fragment (never sent)
    ignored. Userinfo and query are kept as written: they can carry credentials or a tenant.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS or not parts.hostname:
            return None
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    userinfo = parts.netloc.rpartition("@")[0]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return scheme, userinfo, parts.hostname.lower(), port, path, parts.query


def _shown(url: Tuple[str, str, str, int, str, str]) -> str:
    """A normalised traces URL for the log, without its userinfo or query."""
    scheme, _, host, port, path, _ = url
    return f"{scheme}://{host}:{port}{path}"


def _exporter_headers(exporter: Any) -> Optional[Dict[str, str]]:
    """The headers an OTLP/HTTP exporter sends, names lower-cased, or None when unreadable.

    opentelemetry-exporter-otlp-proto-http keeps them on the exporter (`_headers`) before 1.45,
    and on its inner `_client` from 1.45.
    """
    for owner in (exporter, getattr(exporter, "_client", None)):
        headers = getattr(owner, "_headers", None)
        try:
            pairs = headers.items() if isinstance(headers, Mapping) else None
            if pairs is None and isinstance(headers, (list, tuple)):
                pairs = headers
            if pairs is not None:
                return {str(name).strip().lower(): str(value).strip() for name, value in pairs}
        except (TypeError, ValueError):
            return None
    return None


def _strands_otlp_exporter_target() -> Optional[Tuple[str, Dict[str, str]]]:
    """The traces URL and headers Strands' OTLP exporter would send with, or None if unknown.

    Strands' setup_otlp_exporter() builds the upstream OTLP/HTTP span exporter with no arguments,
    so it resolves both from the OTEL_EXPORTER_OTLP_* variables. One is built the same way here,
    read back and shut down, so the answer follows the installed exporter release: the rules for
    empty variables and for merging headers differ between releases.
    """
    if any(os.environ.get(name) for name in _CREDENTIALS_OUTSIDE_HEADERS):
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        probe = OTLPSpanExporter()
    except Exception:
        return None
    try:
        endpoint = getattr(probe, "_endpoint", None)
        headers = _exporter_headers(probe)
    finally:
        try:
            probe.shutdown()
        except Exception:
            pass
    if not isinstance(endpoint, str) or headers is None:
        return None
    return endpoint, headers


def _span_exporters(tracer_provider: Any) -> List[Any]:
    """The exporters behind the span processors already on an SDK tracer provider."""
    active = getattr(tracer_provider, "_active_span_processor", None)
    processors = getattr(active, "_span_processors", None)
    if not isinstance(processors, (tuple, list)):
        return []
    exporters = []
    for processor in processors:
        exporter = getattr(processor, "span_exporter", None)
        if exporter is None:
            # Recent OTel SDKs keep a BatchSpanProcessor's exporter on an inner _batch_processor.
            exporter = getattr(getattr(processor, "_batch_processor", None), "_exporter", None)
        if exporter is not None:
            exporters.append(exporter)
    return exporters


def _strands_otlp_exporter_duplicates(tracer_provider: Any) -> Optional[str]:
    """The traces URL, when Strands' OTLP exporter would only send copies of what the provider sends.

    That is when a span processor on the provider already exports to the same traces URL, and
    sends every header Strands' exporter would send, with the same value: other keys, or a header
    such as x-project-name, can send the spans somewhere else, and keys only Strands' exporter
    carries can be the only ones the collector accepts. Returns None otherwise, and whenever that
    cannot be shown, so Strands' exporter is added as before.

    It also returns None when the provider would replace its exporter on the next
    add_span_processor() call. fi-instrumentation-otel 1.1.0 and earlier mark the exporter of the
    provider register() returns that way; skipping there would leave no exporter for that URL once
    any processor is added, such as Strands' console exporter.
    """
    if getattr(tracer_provider, "_default_processor", False):
        return None
    exporters = _span_exporters(tracer_provider)
    if not exporters:
        return None
    target = _strands_otlp_exporter_target()
    if target is None:
        return None
    endpoint, headers = target
    url = _traces_url(endpoint)
    if url is None:
        return None
    for exporter in exporters:
        if _traces_url(getattr(exporter, "_endpoint", None)) != url:
            continue
        sent = _exporter_headers(exporter)
        if sent is not None and all(sent.get(name) == value for name, value in headers.items()):
            return _shown(url)
    return None


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

        Strands' OTLP span exporter is added for the traces URL the OTEL_EXPORTER_OTLP_* variables
        resolve to (http://localhost:4318/v1/traces when none is set), unless it would only send
        copies of what the tracer provider already sends: see _strands_otlp_exporter_duplicates().

        Args:
            enable_console_exporter: Whether to enable console export.
            enable_metrics: Whether to enable metrics.
        """
        from strands.telemetry import StrandsTelemetry

        # Create telemetry instance with our tracer provider
        telemetry = StrandsTelemetry(tracer_provider=self._tracer_provider)

        # Set up exporters. Skip Strands' OTLP exporter when it would only send copies of what the
        # provider already sends, such as the Future AGI exporter of the provider register()
        # returns: a second exporter there would send every span twice.
        duplicated_url = _strands_otlp_exporter_duplicates(self._tracer_provider)
        if duplicated_url is not None:
            logger.info(
                "traceai-strands: the tracer provider already exports spans to %s with the same "
                "headers; not adding Strands' OTLP exporter, which would send every span there "
                "twice.",
                duplicated_url,
            )
        else:
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

        # Option 1: Use with fi_instrumentation. Spans go to Future AGI through
        # register()'s exporter; no OTEL_EXPORTER_OTLP_* setting is needed for
        # that. Strands' own OTLP exporter is still added, for the endpoint
        # those variables resolve to (localhost:4318 when unset).
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
        otlp_endpoint: OTLP endpoint URL for trace export, stored in the
            OTEL_EXPORTER_OTLP_ENDPOINT environment variable: a base URL that
            Strands' exporter appends /v1/traces to, unless
            OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set. If not provided, the
            OTEL_EXPORTER_OTLP_* environment variables apply. Strands' OTLP
            exporter is not added when the tracer provider already exports to
            the same traces URL with every header it would send, and keeps that
            exporter when another processor is added (for example register()'s
            Future AGI exporter on fi-instrumentation-otel releases with the
            TH-8727 fix), so spans are not sent there twice.
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
