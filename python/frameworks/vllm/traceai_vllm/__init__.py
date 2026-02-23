"""TraceAI instrumentation for vLLM.

This module provides OpenTelemetry instrumentation for vLLM,
enabling comprehensive observability for vLLM API calls.
vLLM uses an OpenAI-compatible API.

Example usage:

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from traceai_vllm import VLLMInstrumentor

    # Setup TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="vllm-app",
    )

    # Instrument vLLM
    VLLMInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use OpenAI client with vLLM
    from openai import OpenAI

    client = OpenAI(
        api_key="token",  # vLLM doesn't require real key
        base_url="http://localhost:8000/v1",  # Your vLLM server
    )
    response = client.chat.completions.create(
        model="meta-llama/Llama-2-7b-chat-hf",
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
