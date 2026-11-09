"""Legacy chat-only vLLM client instrumentation.

VLLMInstrumentor remains available for OpenAI >= 1.0.0. New applications should
use traceai_openai.OpenAIInstrumentor and set_client_provider(client,
provider="vllm") with OpenAI >= 1.69.0. See the package README for migration.
This package does not instrument the vLLM server or inference engine.
"""

import logging
import warnings
from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_vllm.version import __version__
from traceai_vllm._wrappers import (
    _CompletionsWrapper,
    _AsyncCompletionsWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("openai >= 1.0.0",)

__all__ = [
    "VLLMInstrumentor",
    "__version__",
]


class VLLMInstrumentor(BaseInstrumentor):
    """An instrumentor for vLLM (OpenAI-compatible API)."""

    __slots__ = (
        "_original_completions_create",
        "_original_async_completions_create",
        "_tracer",
        "_vllm_base_urls",
    )

    def __init__(self, vllm_base_urls: list[str] | None = None):
        """Initialize the instrumentor.

        Args:
            vllm_base_urls: List of vLLM server base URLs to instrument.
                           Defaults to ["localhost:8000"] if not provided.
        """
        super().__init__()
        self._vllm_base_urls = vllm_base_urls or ["localhost:8000"]

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        warnings.warn(
            "VLLMInstrumentor is deprecated. Use traceai_openai.OpenAIInstrumentor "
            "and set_client_provider(client, provider='vllm') instead "
            "(requires OpenAI >= 1.69.0). When both instrumentors are enabled, "
            "OpenAIInstrumentor owns tracing and its tracer_provider and "
            "TraceConfig apply.",
            DeprecationWarning,
            stacklevel=3,
        )
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
            wrapper=_CompletionsWrapper(tracer=self._tracer, vllm_base_urls=self._vllm_base_urls),
        )

        # Wrap asynchronous completions
        self._original_async_completions_create = AsyncCompletions.create
        wrap_function_wrapper(
            module="openai.resources.chat",
            name="AsyncCompletions.create",
            wrapper=_AsyncCompletionsWrapper(tracer=self._tracer, vllm_base_urls=self._vllm_base_urls),
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
