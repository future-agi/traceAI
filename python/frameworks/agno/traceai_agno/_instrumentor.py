"""Configure the maintained OpenInference Agno instrumentor and OTLP export.

Agno's own tracing setup uses this same instrumentor. This module does not
install a second set of framework/model wrappers or transform emitted spans.
"""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import TracerProvider

if TYPE_CHECKING:
    from openinference.instrumentation import TraceConfig
    from openinference.instrumentation.agno import AgnoInstrumentor


class AgnoInstrumentorWrapper:
    """Process-wide Agno instrumentation. Configure once before running agents."""

    _instance: AgnoInstrumentorWrapper | None = None
    _lock = RLock()
    _tracer_provider: TracerProvider | None
    _agno_instrumentor: AgnoInstrumentor | None
    _owned_provider: SDKTracerProvider | None
    _configuration: (
        tuple[
            TracerProvider | None,
            str | None,
            dict[str, str],
            str | None,
            TraceConfig | None,
        ]
        | None
    )

    def __new__(cls) -> AgnoInstrumentorWrapper:
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._tracer_provider = None
                instance._agno_instrumentor = None
                instance._owned_provider = None
                instance._configuration = None
                cls._instance = instance
            return cls._instance

    def instrument(
        self,
        tracer_provider: TracerProvider | None = None,
        otlp_endpoint: str | None = None,
        otlp_headers: dict[str, str] | None = None,
        *,
        project_name: str | None = None,
        config: TraceConfig | None = None,
    ) -> AgnoInstrumentorWrapper:
        """Use an existing provider OR create an OTLP/HTTP protobuf exporter.

        Explicit providers are passed directly to OpenInference, never installed
        globally. Endpoint configuration owns a private provider. With no
        arguments, an already configured global provider is required.
        ``config`` is OpenInference's TraceConfig, not fi_instrumentation's.
        """
        with self._lock:
            configuration = (
                tracer_provider,
                otlp_endpoint,
                dict(otlp_headers or {}),
                project_name,
                config,
            )
            if self._agno_instrumentor is not None and not self.is_instrumented:
                raise RuntimeError(
                    "Agno instrumentation was removed externally; call uninstrument before configuring again."
                )
            if self.is_instrumented:
                if all(
                    value is None
                    for value in (
                        tracer_provider,
                        otlp_endpoint,
                        otlp_headers,
                        project_name,
                        config,
                    )
                ):
                    return self
                if configuration != self._configuration:
                    raise ValueError(
                        "Agno is already configured; uninstrument before changing its configuration."
                    )
                return self
            if tracer_provider is not None and any(
                value is not None
                for value in (otlp_endpoint, otlp_headers, project_name)
            ):
                raise ValueError(
                    "Configure export/project on tracer_provider OR supply otlp_endpoint, not both."
                )
            if otlp_endpoint is None and (
                otlp_headers is not None or project_name is not None
            ):
                raise ValueError("otlp_headers and project_name require otlp_endpoint.")

            try:
                from openinference.instrumentation.agno import AgnoInstrumentor
            except ImportError as exc:
                raise ImportError(
                    "Install openinference-instrumentation-agno and Agno with: "
                    "pip install 'traceai-agno[agno]'"
                ) from exc
            delegate = AgnoInstrumentor()
            if delegate.is_instrumented_by_opentelemetry:
                raise RuntimeError(
                    "Agno is already instrumented outside traceai-agno. Use only one setup path; "
                    "do not combine configure_agno_tracing with Agno setup_tracing or AgnoInstrumentor."
                )

            owned_provider = None
            provider: TracerProvider
            if otlp_endpoint is not None:
                owned_provider = setup_traceai_exporter(
                    endpoint=otlp_endpoint,
                    headers=otlp_headers,
                    project_name=project_name,
                    set_global=False,
                )
                provider = owned_provider
            else:
                provider = (
                    tracer_provider
                    if tracer_provider is not None
                    else trace.get_tracer_provider()
                )
                if isinstance(
                    provider, (trace.ProxyTracerProvider, trace.NoOpTracerProvider)
                ):
                    raise ValueError(
                        "Supply a configured tracer_provider or otlp_endpoint before tracing Agno."
                    )
            try:
                delegate.instrument(
                    tracer_provider=provider,
                    config=config,
                    raise_exception_on_conflict=True,
                )
                if not delegate.is_instrumented_by_opentelemetry:
                    raise RuntimeError(
                        "OpenInference did not activate Agno instrumentation."
                    )
            except Exception:
                if owned_provider is not None:
                    owned_provider.shutdown()
                raise
            self._agno_instrumentor = delegate
            self._tracer_provider = provider
            self._owned_provider = owned_provider
            self._configuration = configuration
            return self

    def uninstrument(self) -> None:
        """Remove our instrumentation and close only a provider we created.

        Call after in-flight runs finish. Caller-supplied providers remain usable
        and must be flushed/shut down by their owner.
        """
        with self._lock:
            if self._agno_instrumentor is not None and self.is_instrumented:
                self._agno_instrumentor.uninstrument()
            if self._owned_provider is not None:
                self._owned_provider.shutdown()
            self._agno_instrumentor = None
            self._tracer_provider = None
            self._owned_provider = None
            self._configuration = None

    @property
    def is_instrumented(self) -> bool:
        """Whether the delegated instrumentor is currently active."""
        return bool(
            self._agno_instrumentor is not None
            and self._agno_instrumentor.is_instrumented_by_opentelemetry
        )

    @property
    def tracer_provider(self) -> TracerProvider | None:
        """The configured provider, for force_flush before process exit."""
        return self._tracer_provider


def configure_agno_tracing(
    tracer_provider: TracerProvider | None = None,
    otlp_endpoint: str | None = None,
    otlp_headers: dict[str, str] | None = None,
    project_name: str | None = None,
    *,
    config: TraceConfig | None = None,
) -> AgnoInstrumentorWrapper:
    """Configure Agno using an existing provider or a complete OTLP traces URL.

    For Future AGI direct export, use https://api.futureagi.com/tracer/v1/traces,
    headers X-Api-Key and X-Secret-Key, and a project_name. Project attributes
    belong to the resource, not authentication headers. Content controls are
    supplied through OpenInference's TraceConfig or OPENINFERENCE_* variables.
    """
    return AgnoInstrumentorWrapper().instrument(
        tracer_provider=tracer_provider,
        otlp_endpoint=otlp_endpoint,
        otlp_headers=otlp_headers,
        project_name=project_name,
        config=config,
    )


def setup_traceai_exporter(
    endpoint: str,
    headers: dict[str, str] | None = None,
    use_batch_processor: bool = True,
    service_name: str = "agno-agent",
    *,
    project_name: str | None = None,
    set_global: bool = True,
) -> SDKTracerProvider:
    """Create an OTLP/HTTP protobuf provider with Future AGI resource attributes.

    ``endpoint`` is the full traces URL. ``project_name`` defaults to service_name.
    The caller owns this provider and must shut it down. ``set_global=True``
    preserves the helper's historical behavior; use False for a private provider.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if not endpoint:
        raise ValueError("endpoint must be a complete OTLP traces URL.")
    resource = Resource.create(
        {
            "service.name": service_name,
            "project_name": project_name or service_name,
            "project_type": "observe",
            "semantic_convention": "openinference",
        }
    )
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=dict(headers or {}))
    provider = SDKTracerProvider(resource=resource)
    processor = (
        BatchSpanProcessor(exporter)
        if use_batch_processor
        else SimpleSpanProcessor(exporter)
    )
    provider.add_span_processor(processor)
    if set_global:
        trace.set_tracer_provider(provider)
    return provider
