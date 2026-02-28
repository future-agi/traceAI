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

from traceai_cohere._wrappers import (
    _AsyncChatWrapper,
    _AsyncEmbedWrapper,
    _AsyncRerankWrapper,
    _ChatStreamWrapper,
    _ChatWrapper,
    _EmbedWrapper,
    _RerankWrapper,
)
from traceai_cohere.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("cohere >= 5.0.0",)


class CohereInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Cohere."""

    __slots__ = (
        "_original_chat",
        "_original_async_chat",
        "_original_chat_stream",
        "_original_async_chat_stream",
        "_original_embed",
        "_original_async_embed",
        "_original_rerank",
        "_original_async_rerank",
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

        # Import cohere client
        cohere_module = import_module("cohere")

        # Wrap Client methods
        # Chat
        if hasattr(cohere_module.Client, "chat"):
            self._original_chat = cohere_module.Client.chat
            wrap_function_wrapper(
                module="cohere",
                name="Client.chat",
                wrapper=_ChatWrapper(tracer=self._tracer),
            )

        # Chat stream
        if hasattr(cohere_module.Client, "chat_stream"):
            self._original_chat_stream = cohere_module.Client.chat_stream
            wrap_function_wrapper(
                module="cohere",
                name="Client.chat_stream",
                wrapper=_ChatStreamWrapper(tracer=self._tracer),
            )

        # Embed
        if hasattr(cohere_module.Client, "embed"):
            self._original_embed = cohere_module.Client.embed
            wrap_function_wrapper(
                module="cohere",
                name="Client.embed",
                wrapper=_EmbedWrapper(tracer=self._tracer),
            )

        # Rerank
        if hasattr(cohere_module.Client, "rerank"):
            self._original_rerank = cohere_module.Client.rerank
            wrap_function_wrapper(
                module="cohere",
                name="Client.rerank",
                wrapper=_RerankWrapper(tracer=self._tracer),
            )

        # Wrap AsyncClient methods
        if hasattr(cohere_module, "AsyncClient"):
            if hasattr(cohere_module.AsyncClient, "chat"):
                self._original_async_chat = cohere_module.AsyncClient.chat
                wrap_function_wrapper(
                    module="cohere",
                    name="AsyncClient.chat",
                    wrapper=_AsyncChatWrapper(tracer=self._tracer),
                )

            if hasattr(cohere_module.AsyncClient, "embed"):
                self._original_async_embed = cohere_module.AsyncClient.embed
                wrap_function_wrapper(
                    module="cohere",
                    name="AsyncClient.embed",
                    wrapper=_AsyncEmbedWrapper(tracer=self._tracer),
                )

            if hasattr(cohere_module.AsyncClient, "rerank"):
                self._original_async_rerank = cohere_module.AsyncClient.rerank
                wrap_function_wrapper(
                    module="cohere",
                    name="AsyncClient.rerank",
                    wrapper=_AsyncRerankWrapper(tracer=self._tracer),
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
        cohere_module = import_module("cohere")

        if hasattr(self, "_original_chat"):
            cohere_module.Client.chat = self._original_chat
        if hasattr(self, "_original_chat_stream"):
            cohere_module.Client.chat_stream = self._original_chat_stream
        if hasattr(self, "_original_embed"):
            cohere_module.Client.embed = self._original_embed
        if hasattr(self, "_original_rerank"):
            cohere_module.Client.rerank = self._original_rerank

        if hasattr(cohere_module, "AsyncClient"):
            if hasattr(self, "_original_async_chat"):
                cohere_module.AsyncClient.chat = self._original_async_chat
            if hasattr(self, "_original_async_embed"):
                cohere_module.AsyncClient.embed = self._original_async_embed
            if hasattr(self, "_original_async_rerank"):
                cohere_module.AsyncClient.rerank = self._original_async_rerank
