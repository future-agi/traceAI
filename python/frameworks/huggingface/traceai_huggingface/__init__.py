import logging
from importlib import import_module
from typing import Any, Collection

logger = logging.getLogger(__name__)

try:
    from fi.evals import Protect
except ImportError:
    logger.debug("ai-evaluation is not installed")
    Protect = None

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_huggingface._wrappers import (
    _AsyncChatCompletionWrapper,
    _AsyncFeatureExtractionWrapper,
    _AsyncTextGenerationWrapper,
    _ChatCompletionWrapper,
    _FeatureExtractionWrapper,
    _TextGenerationWrapper,
)
from traceai_huggingface.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("huggingface-hub >= 0.20.0",)


class HuggingFaceInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for HuggingFace Inference API."""

    __slots__ = (
        "_original_text_generation",
        "_original_async_text_generation",
        "_original_chat_completion",
        "_original_async_chat_completion",
        "_original_feature_extraction",
        "_original_async_feature_extraction",
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

        # Import huggingface_hub module
        try:
            huggingface_hub = import_module("huggingface_hub")
        except ImportError:
            logger.warning("huggingface_hub is not installed")
            return

        # Wrap InferenceClient methods
        if hasattr(huggingface_hub, "InferenceClient"):
            # Text generation
            if hasattr(huggingface_hub.InferenceClient, "text_generation"):
                self._original_text_generation = huggingface_hub.InferenceClient.text_generation
                wrap_function_wrapper(
                    module="huggingface_hub",
                    name="InferenceClient.text_generation",
                    wrapper=_TextGenerationWrapper(tracer=self._tracer),
                )

            # Chat completion
            if hasattr(huggingface_hub.InferenceClient, "chat_completion"):
                self._original_chat_completion = huggingface_hub.InferenceClient.chat_completion
                wrap_function_wrapper(
                    module="huggingface_hub",
                    name="InferenceClient.chat_completion",
                    wrapper=_ChatCompletionWrapper(tracer=self._tracer),
                )

            # Feature extraction (embeddings)
            if hasattr(huggingface_hub.InferenceClient, "feature_extraction"):
                self._original_feature_extraction = huggingface_hub.InferenceClient.feature_extraction
                wrap_function_wrapper(
                    module="huggingface_hub",
                    name="InferenceClient.feature_extraction",
                    wrapper=_FeatureExtractionWrapper(tracer=self._tracer),
                )

        # Wrap AsyncInferenceClient methods
        if hasattr(huggingface_hub, "AsyncInferenceClient"):
            # Async text generation
            if hasattr(huggingface_hub.AsyncInferenceClient, "text_generation"):
                self._original_async_text_generation = huggingface_hub.AsyncInferenceClient.text_generation
                wrap_function_wrapper(
                    module="huggingface_hub",
                    name="AsyncInferenceClient.text_generation",
                    wrapper=_AsyncTextGenerationWrapper(tracer=self._tracer),
                )

            # Async chat completion
            if hasattr(huggingface_hub.AsyncInferenceClient, "chat_completion"):
                self._original_async_chat_completion = huggingface_hub.AsyncInferenceClient.chat_completion
                wrap_function_wrapper(
                    module="huggingface_hub",
                    name="AsyncInferenceClient.chat_completion",
                    wrapper=_AsyncChatCompletionWrapper(tracer=self._tracer),
                )

            # Async feature extraction
            if hasattr(huggingface_hub.AsyncInferenceClient, "feature_extraction"):
                self._original_async_feature_extraction = huggingface_hub.AsyncInferenceClient.feature_extraction
                wrap_function_wrapper(
                    module="huggingface_hub",
                    name="AsyncInferenceClient.feature_extraction",
                    wrapper=_AsyncFeatureExtractionWrapper(tracer=self._tracer),
                )

        # Wrap Protect if available
        if Protect is not None:
            self._original_protect = Protect.protect
            wrap_function_wrapper(
                module="fi.evals",
                name="Protect.protect",
                wrapper=GuardrailProtectWrapper(tracer=self._tracer),
            )
        else:
            self._original_protect = None

    def _uninstrument(self, **kwargs: Any) -> None:
        try:
            huggingface_hub = import_module("huggingface_hub")
        except ImportError:
            return

        if hasattr(huggingface_hub, "InferenceClient"):
            if hasattr(self, "_original_text_generation"):
                huggingface_hub.InferenceClient.text_generation = self._original_text_generation
            if hasattr(self, "_original_chat_completion"):
                huggingface_hub.InferenceClient.chat_completion = self._original_chat_completion
            if hasattr(self, "_original_feature_extraction"):
                huggingface_hub.InferenceClient.feature_extraction = self._original_feature_extraction

        if hasattr(huggingface_hub, "AsyncInferenceClient"):
            if hasattr(self, "_original_async_text_generation"):
                huggingface_hub.AsyncInferenceClient.text_generation = self._original_async_text_generation
            if hasattr(self, "_original_async_chat_completion"):
                huggingface_hub.AsyncInferenceClient.chat_completion = self._original_async_chat_completion
            if hasattr(self, "_original_async_feature_extraction"):
                huggingface_hub.AsyncInferenceClient.feature_extraction = self._original_async_feature_extraction
