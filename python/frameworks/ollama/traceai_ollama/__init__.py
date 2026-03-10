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

from traceai_ollama._wrappers import (
    _AsyncChatWrapper,
    _AsyncEmbedWrapper,
    _AsyncGenerateWrapper,
    _ChatWrapper,
    _EmbedWrapper,
    _GenerateWrapper,
)
from traceai_ollama.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("ollama >= 0.3.0",)


class OllamaInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Ollama."""

    __slots__ = (
        "_original_chat",
        "_original_async_chat",
        "_original_generate",
        "_original_async_generate",
        "_original_embed",
        "_original_async_embed",
        "_original_embeddings",
        "_original_async_embeddings",
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

        ollama_module = import_module("ollama")

        # Wrap synchronous Client methods
        self._original_chat = ollama_module.Client.chat
        wrap_function_wrapper(
            module="ollama",
            name="Client.chat",
            wrapper=_ChatWrapper(tracer=self._tracer),
        )

        self._original_generate = ollama_module.Client.generate
        wrap_function_wrapper(
            module="ollama",
            name="Client.generate",
            wrapper=_GenerateWrapper(tracer=self._tracer),
        )

        # Ollama uses 'embed' for single and 'embeddings' for batch
        if hasattr(ollama_module.Client, "embed"):
            self._original_embed = ollama_module.Client.embed
            wrap_function_wrapper(
                module="ollama",
                name="Client.embed",
                wrapper=_EmbedWrapper(tracer=self._tracer),
            )

        if hasattr(ollama_module.Client, "embeddings"):
            self._original_embeddings = ollama_module.Client.embeddings
            wrap_function_wrapper(
                module="ollama",
                name="Client.embeddings",
                wrapper=_EmbedWrapper(tracer=self._tracer),
            )

        # Wrap asynchronous AsyncClient methods
        self._original_async_chat = ollama_module.AsyncClient.chat
        wrap_function_wrapper(
            module="ollama",
            name="AsyncClient.chat",
            wrapper=_AsyncChatWrapper(tracer=self._tracer),
        )

        self._original_async_generate = ollama_module.AsyncClient.generate
        wrap_function_wrapper(
            module="ollama",
            name="AsyncClient.generate",
            wrapper=_AsyncGenerateWrapper(tracer=self._tracer),
        )

        if hasattr(ollama_module.AsyncClient, "embed"):
            self._original_async_embed = ollama_module.AsyncClient.embed
            wrap_function_wrapper(
                module="ollama",
                name="AsyncClient.embed",
                wrapper=_AsyncEmbedWrapper(tracer=self._tracer),
            )

        if hasattr(ollama_module.AsyncClient, "embeddings"):
            self._original_async_embeddings = ollama_module.AsyncClient.embeddings
            wrap_function_wrapper(
                module="ollama",
                name="AsyncClient.embeddings",
                wrapper=_AsyncEmbedWrapper(tracer=self._tracer),
            )

        # Wrap module-level functions
        if hasattr(ollama_module, "chat"):
            wrap_function_wrapper(
                module="ollama",
                name="chat",
                wrapper=_ChatWrapper(tracer=self._tracer),
            )

        if hasattr(ollama_module, "generate"):
            wrap_function_wrapper(
                module="ollama",
                name="generate",
                wrapper=_GenerateWrapper(tracer=self._tracer),
            )

        if hasattr(ollama_module, "embed"):
            wrap_function_wrapper(
                module="ollama",
                name="embed",
                wrapper=_EmbedWrapper(tracer=self._tracer),
            )

        if hasattr(ollama_module, "embeddings"):
            wrap_function_wrapper(
                module="ollama",
                name="embeddings",
                wrapper=_EmbedWrapper(tracer=self._tracer),
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
        ollama_module = import_module("ollama")

        if hasattr(self, "_original_chat"):
            ollama_module.Client.chat = self._original_chat
        if hasattr(self, "_original_generate"):
            ollama_module.Client.generate = self._original_generate
        if hasattr(self, "_original_embed"):
            ollama_module.Client.embed = self._original_embed
        if hasattr(self, "_original_embeddings"):
            ollama_module.Client.embeddings = self._original_embeddings
        if hasattr(self, "_original_async_chat"):
            ollama_module.AsyncClient.chat = self._original_async_chat
        if hasattr(self, "_original_async_generate"):
            ollama_module.AsyncClient.generate = self._original_async_generate
        if hasattr(self, "_original_async_embed"):
            ollama_module.AsyncClient.embed = self._original_async_embed
        if hasattr(self, "_original_async_embeddings"):
            ollama_module.AsyncClient.embeddings = self._original_async_embeddings
