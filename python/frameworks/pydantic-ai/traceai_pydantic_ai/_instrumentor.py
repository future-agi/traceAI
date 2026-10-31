"""Configure Pydantic AI's native spans without patching Agent or model methods."""

import inspect
from collections.abc import Collection
from dataclasses import replace
from typing import Any

from fi_instrumentation.instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.metrics import NoOpMeterProvider

from ._native import NativeTracerProvider

_CONTENT_FLAGS = (
    "hide_inputs",
    "hide_outputs",
    "hide_input_messages",
    "hide_output_messages",
    "hide_input_text",
    "hide_output_text",
    "hide_input_images",
)


class PydanticAIInstrumentor(BaseInstrumentor):
    """Enable native agent, model, and tool tracing on the supplied provider.

    ``include_content=False`` is the default. A TraceConfig content-hide flag
    disables all native content, since native message JSON mixes directions and
    modalities. ``use_builtin`` remains accepted; both values use the native route.
    Per-agent explicit ``instrument`` settings continue to take precedence.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        # Both the slim and the full distribution install this distribution.
        return ("pydantic-ai-slim >= 1.0.0",)

    def _instrument(self, **kwargs: Any) -> None:
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        include_content = kwargs.get("include_content", False)
        if not isinstance(include_content, bool):
            raise TypeError("include_content must be a bool")
        include_content = include_content and not any(
            getattr(config, flag) for flag in _CONTENT_FLAGS
        )
        if not include_content:
            config = replace(config, _hide_error_details=True)

        provider = kwargs.get("tracer_provider")
        if provider is None:
            provider = trace.get_tracer_provider()
        adapter = NativeTracerProvider(provider, config, include_content)
        options = {
            "tracer_provider": adapter,
            "meter_provider": NoOpMeterProvider(),
            "include_content": include_content,
            "include_binary_content": False,
        }
        # Native defaults select the supported span-message format, not logs.
        # Older releases lack these switches; the adapter filters their payloads.
        parameters = inspect.signature(InstrumentationSettings).parameters
        if "include_model_request_parameters" in parameters:
            options["include_model_request_parameters"] = False
        if "use_aggregated_usage_attribute_names" in parameters:
            options["use_aggregated_usage_attribute_names"] = True
        settings = InstrumentationSettings(**options)
        # Pydantic AI exposes a setter but no getter. Only restore our own value.
        previous = Agent._instrument_default
        Agent.instrument_all(settings)
        self._settings = settings
        self._previous = previous
        self._adapter = adapter

    def _uninstrument(self, **kwargs: Any) -> None:
        from pydantic_ai import Agent

        if Agent._instrument_default is self._settings:
            Agent.instrument_all(self._previous)
        # Cached native settings cannot start new spans after teardown. Spans
        # already in flight still finish normally on their original provider.
        self._adapter.enabled = False
        self._settings = None
        self._previous = None

    @property
    def is_instrumented(self) -> bool:
        return self.is_instrumented_by_opentelemetry


def instrument_pydantic_ai(
    tracer_provider: trace.TracerProvider | None = None,
    use_builtin: bool = False,
    *,
    include_content: bool = False,
    config: TraceConfig | None = None,
) -> PydanticAIInstrumentor:
    """Enable native tracing; the legacy ``use_builtin`` argument is a no-op."""
    instrumentor = PydanticAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=tracer_provider,
        use_builtin=use_builtin,
        include_content=include_content,
        config=config,
    )
    return instrumentor
