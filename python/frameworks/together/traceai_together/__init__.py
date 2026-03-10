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

from traceai_together._wrappers import (
    _AsyncChatCompletionsWrapper,
    _AsyncCompletionsWrapper,
    _AsyncEmbeddingsWrapper,
    _ChatCompletionsWrapper,
    _CompletionsWrapper,
    _EmbeddingsWrapper,
)
from traceai_together.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("together >= 1.0.0",)


class TogetherInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Together AI."""

    __slots__ = (
        "_original_chat_completions_create",
        "_original_async_chat_completions_create",
        "_original_completions_create",
        "_original_async_completions_create",
        "_original_embeddings_create",
        "_original_async_embeddings_create",
        "_original_protect",
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

        # Import together client
        together_module = import_module("together")

        # Wrap Together client methods
        # Together uses a structure like:
        # - together.Together (sync client)
        # - together.AsyncTogether (async client)
        # Each has: chat.completions.create, completions.create, embeddings.create

        # Sync client - chat.completions.create
        try:
            if hasattr(together_module, "Together"):
                together_client = together_module.Together

                # The Together client uses nested resources
                # We need to wrap the create methods on the resource classes

                # Wrap chat.completions.create
                if hasattr(together_client, "chat"):
                    # Together SDK uses resources.chat.ChatCompletions
                    try:
                        from together.resources.chat import ChatCompletions
                        self._original_chat_completions_create = ChatCompletions.create
                        wrap_function_wrapper(
                            module="together.resources.chat",
                            name="ChatCompletions.create",
                            wrapper=_ChatCompletionsWrapper(tracer=self._tracer),
                        )
                    except (ImportError, AttributeError) as e:
                        logger.debug(f"Could not wrap chat.completions.create: {e}")

                # Wrap completions.create
                try:
                    from together.resources.completions import Completions
                    self._original_completions_create = Completions.create
                    wrap_function_wrapper(
                        module="together.resources.completions",
                        name="Completions.create",
                        wrapper=_CompletionsWrapper(tracer=self._tracer),
                    )
                except (ImportError, AttributeError) as e:
                    logger.debug(f"Could not wrap completions.create: {e}")

                # Wrap embeddings.create
                try:
                    from together.resources.embeddings import Embeddings
                    self._original_embeddings_create = Embeddings.create
                    wrap_function_wrapper(
                        module="together.resources.embeddings",
                        name="Embeddings.create",
                        wrapper=_EmbeddingsWrapper(tracer=self._tracer),
                    )
                except (ImportError, AttributeError) as e:
                    logger.debug(f"Could not wrap embeddings.create: {e}")

        except Exception as e:
            logger.exception(f"Error wrapping sync Together client: {e}")

        # Async client
        try:
            if hasattr(together_module, "AsyncTogether"):
                # Wrap async chat.completions.create
                try:
                    from together.resources.chat import AsyncChatCompletions
                    self._original_async_chat_completions_create = AsyncChatCompletions.create
                    wrap_function_wrapper(
                        module="together.resources.chat",
                        name="AsyncChatCompletions.create",
                        wrapper=_AsyncChatCompletionsWrapper(tracer=self._tracer),
                    )
                except (ImportError, AttributeError) as e:
                    logger.debug(f"Could not wrap async chat.completions.create: {e}")

                # Wrap async completions.create
                try:
                    from together.resources.completions import AsyncCompletions
                    self._original_async_completions_create = AsyncCompletions.create
                    wrap_function_wrapper(
                        module="together.resources.completions",
                        name="AsyncCompletions.create",
                        wrapper=_AsyncCompletionsWrapper(tracer=self._tracer),
                    )
                except (ImportError, AttributeError) as e:
                    logger.debug(f"Could not wrap async completions.create: {e}")

                # Wrap async embeddings.create
                try:
                    from together.resources.embeddings import AsyncEmbeddings
                    self._original_async_embeddings_create = AsyncEmbeddings.create
                    wrap_function_wrapper(
                        module="together.resources.embeddings",
                        name="AsyncEmbeddings.create",
                        wrapper=_AsyncEmbeddingsWrapper(tracer=self._tracer),
                    )
                except (ImportError, AttributeError) as e:
                    logger.debug(f"Could not wrap async embeddings.create: {e}")

        except Exception as e:
            logger.exception(f"Error wrapping async Together client: {e}")

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
        """Remove instrumentation from Together AI client."""
        try:
            # Restore sync methods
            try:
                from together.resources.chat import ChatCompletions
                if hasattr(self, "_original_chat_completions_create"):
                    ChatCompletions.create = self._original_chat_completions_create
            except (ImportError, AttributeError):
                pass

            try:
                from together.resources.completions import Completions
                if hasattr(self, "_original_completions_create"):
                    Completions.create = self._original_completions_create
            except (ImportError, AttributeError):
                pass

            try:
                from together.resources.embeddings import Embeddings
                if hasattr(self, "_original_embeddings_create"):
                    Embeddings.create = self._original_embeddings_create
            except (ImportError, AttributeError):
                pass

            # Restore async methods
            try:
                from together.resources.chat import AsyncChatCompletions
                if hasattr(self, "_original_async_chat_completions_create"):
                    AsyncChatCompletions.create = self._original_async_chat_completions_create
            except (ImportError, AttributeError):
                pass

            try:
                from together.resources.completions import AsyncCompletions
                if hasattr(self, "_original_async_completions_create"):
                    AsyncCompletions.create = self._original_async_completions_create
            except (ImportError, AttributeError):
                pass

            try:
                from together.resources.embeddings import AsyncEmbeddings
                if hasattr(self, "_original_async_embeddings_create"):
                    AsyncEmbeddings.create = self._original_async_embeddings_create
            except (ImportError, AttributeError):
                pass

        except Exception as e:
            logger.exception(f"Error uninstrumenting Together client: {e}")
