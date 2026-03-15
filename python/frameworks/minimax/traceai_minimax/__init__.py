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

from traceai_minimax._wrappers import (
    _AsyncChatCompletionWrapper,
    _ChatCompletionWrapper,
)
from traceai_minimax.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("openai >= 1.0.0",)


class MiniMaxInstrumentor(BaseInstrumentor):
    """
    OpenTelemetry instrumentor for MiniMax.

    MiniMax uses the OpenAI SDK with a custom base_url (https://api.minimax.io/v1).
    This instrumentor wraps OpenAI client methods and only instruments calls
    when the client is configured with a MiniMax base URL.

    Supports:
    - MiniMax-M2.5 (204K context window)
    - MiniMax-M2.5-highspeed (204K context window, faster inference)
    - Streaming and non-streaming responses
    - Function/tool calling
    """

    __slots__ = (
        "_original_chat_create",
        "_original_async_chat_create",
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

        # Import openai module
        try:
            openai_module = import_module("openai")
        except ImportError:
            logger.warning("openai package not installed, MiniMax instrumentation skipped")
            return

        # Wrap OpenAI client chat.completions.create
        # The wrapper checks if the client is configured for MiniMax
        try:
            # Sync client
            if hasattr(openai_module, "OpenAI"):
                from openai.resources.chat import completions

                self._original_chat_create = completions.Completions.create
                wrap_function_wrapper(
                    module="openai.resources.chat.completions",
                    name="Completions.create",
                    wrapper=_ChatCompletionWrapper(tracer=self._tracer),
                )

            # Async client
            if hasattr(openai_module, "AsyncOpenAI"):
                from openai.resources.chat import completions as async_completions

                self._original_async_chat_create = async_completions.AsyncCompletions.create
                wrap_function_wrapper(
                    module="openai.resources.chat.completions",
                    name="AsyncCompletions.create",
                    wrapper=_AsyncChatCompletionWrapper(tracer=self._tracer),
                )
        except Exception as e:
            logger.warning(f"Failed to instrument OpenAI for MiniMax: {e}")

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
            from openai.resources.chat import completions

            if hasattr(self, "_original_chat_create"):
                completions.Completions.create = self._original_chat_create
            if hasattr(self, "_original_async_chat_create"):
                completions.AsyncCompletions.create = self._original_async_chat_create
        except Exception as e:
            logger.warning(f"Failed to uninstrument: {e}")
