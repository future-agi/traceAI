"""TraceAI instrumentation for Cerebras Cloud SDK.

This module provides OpenTelemetry instrumentation for the Cerebras Cloud SDK,
enabling comprehensive observability for Cerebras LLM API calls.

Example usage:

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from traceai_cerebras import CerebrasInstrumentor

    # Setup TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="cerebras-app",
    )

    # Instrument Cerebras
    CerebrasInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use Cerebras normally
    from cerebras.cloud.sdk import Cerebras

    client = Cerebras()
    response = client.chat.completions.create(
        model="llama3.1-8b",
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

from traceai_cerebras.version import __version__
from traceai_cerebras._wrappers import (
    _CompletionsWrapper,
    _AsyncCompletionsWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("cerebras-cloud-sdk >= 1.0.0",)

__all__ = [
    "CerebrasInstrumentor",
    "__version__",
]


class CerebrasInstrumentor(BaseInstrumentor):
    """An instrumentor for the Cerebras Cloud SDK."""

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

        # Import Cerebras SDK modules
        try:
            from cerebras.cloud.sdk.resources.chat.completions import (
                CompletionsResource,
                AsyncCompletionsResource,
            )
        except ImportError:
            logger.warning(
                "cerebras-cloud-sdk is not installed. "
                "Install with: pip install cerebras-cloud-sdk"
            )
            return

        # Wrap synchronous completions
        self._original_completions_create = CompletionsResource.create
        wrap_function_wrapper(
            module="cerebras.cloud.sdk.resources.chat.completions",
            name="CompletionsResource.create",
            wrapper=_CompletionsWrapper(tracer=self._tracer),
        )

        # Wrap asynchronous completions
        self._original_async_completions_create = AsyncCompletionsResource.create
        wrap_function_wrapper(
            module="cerebras.cloud.sdk.resources.chat.completions",
            name="AsyncCompletionsResource.create",
            wrapper=_AsyncCompletionsWrapper(tracer=self._tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        try:
            cerebras_module = import_module("cerebras.cloud.sdk.resources.chat.completions")
            if hasattr(self, "_original_completions_create") and self._original_completions_create is not None:
                cerebras_module.CompletionsResource.create = self._original_completions_create
            if hasattr(self, "_original_async_completions_create") and self._original_async_completions_create is not None:
                cerebras_module.AsyncCompletionsResource.create = self._original_async_completions_create
        except ImportError:
            pass
