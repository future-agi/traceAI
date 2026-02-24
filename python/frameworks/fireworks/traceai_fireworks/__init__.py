"""TraceAI instrumentation for Fireworks AI.

This module provides OpenTelemetry instrumentation for Fireworks AI,
enabling comprehensive observability for Fireworks LLM API calls.
Fireworks uses an OpenAI-compatible API.

Example usage:

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from traceai_fireworks import FireworksInstrumentor

    # Setup TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="fireworks-app",
    )

    # Instrument Fireworks
    FireworksInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use OpenAI client with Fireworks
    from openai import OpenAI

    client = OpenAI(
        api_key="your-fireworks-api-key",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    response = client.chat.completions.create(
        model="accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages=[{"role": "user", "content": "Hello!"}]
    )
"""

import logging
from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_fireworks.version import __version__
from traceai_fireworks._wrappers import (
    _CompletionsWrapper,
    _AsyncCompletionsWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("openai >= 1.0.0",)

__all__ = [
    "FireworksInstrumentor",
    "__version__",
]


class FireworksInstrumentor(BaseInstrumentor):
    """An instrumentor for Fireworks AI (OpenAI-compatible API)."""

    __slots__ = (
        "_original_completions_create",
        "_original_async_completions_create",
        "_tracer",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)

        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        # Import OpenAI SDK modules
        try:
            from openai.resources.chat import Completions, AsyncCompletions
        except ImportError:
            logger.warning(
                "openai is not installed. "
                "Install with: pip install openai"
            )
            return

        # Wrap synchronous completions
        self._original_completions_create = Completions.create
        wrap_function_wrapper(
            module="openai.resources.chat",
            name="Completions.create",
            wrapper=_CompletionsWrapper(tracer=self._tracer),
        )

        # Wrap asynchronous completions
        self._original_async_completions_create = AsyncCompletions.create
        wrap_function_wrapper(
            module="openai.resources.chat",
            name="AsyncCompletions.create",
            wrapper=_AsyncCompletionsWrapper(tracer=self._tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        try:
            openai_module = import_module("openai.resources.chat")
            if hasattr(self, "_original_completions_create") and self._original_completions_create is not None:
                openai_module.Completions.create = self._original_completions_create
            if hasattr(self, "_original_async_completions_create") and self._original_async_completions_create is not None:
                openai_module.AsyncCompletions.create = self._original_async_completions_create
        except ImportError:
            pass
