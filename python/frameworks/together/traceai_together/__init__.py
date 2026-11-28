import logging
from importlib import import_module
from typing import Any, Collection

logger = logging.getLogger(__name__)

try:
    from fi.evals import Protect
except ImportError:
    logger.debug("Optional fi.evals.Protect is unavailable; Protect calls will not be traced.")
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

# (wrapper, ((module, class), ...)) in order of preference.
_TARGETS = (
    (_ChatCompletionsWrapper, (("together.resources.chat", "ChatCompletions"),
                               ("together.resources.chat.completions", "CompletionsResource"))),
    (_CompletionsWrapper, (("together.resources.completions", "Completions"),
                           ("together.resources.completions", "CompletionsResource"))),
    (_EmbeddingsWrapper, (("together.resources.embeddings", "Embeddings"),
                          ("together.resources.embeddings", "EmbeddingsResource"))),
    (_AsyncChatCompletionsWrapper, (("together.resources.chat", "AsyncChatCompletions"),
                                    ("together.resources.chat.completions", "AsyncCompletionsResource"))),
    (_AsyncCompletionsWrapper, (("together.resources.completions", "AsyncCompletions"),
                                ("together.resources.completions", "AsyncCompletionsResource"))),
    (_AsyncEmbeddingsWrapper, (("together.resources.embeddings", "AsyncEmbeddings"),
                               ("together.resources.embeddings", "AsyncEmbeddingsResource"))),
)


class TogetherInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Together AI."""

    __slots__ = (
        "_patched",
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

        # Each resource class has moved between SDK releases: the 1.x layout (ChatCompletions, Completions,
        # Embeddings) and the 3.x layout (CompletionsResource, EmbeddingsResource). The first class that
        # exists in the installed SDK is wrapped.
        self._patched = []
        for wrapper, candidates in _TARGETS:
            for module_name, class_name in candidates:
                try:
                    target = getattr(import_module(module_name), class_name)
                    original = target.__dict__["create"]
                except (ImportError, AttributeError, KeyError):
                    continue
                try:
                    wrap_function_wrapper(module=module_name, name=f"{class_name}.create", wrapper=wrapper(tracer=self._tracer))
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"Could not wrap {module_name}.{class_name}.create: {e}")
                    continue
                self._patched.append((target, original))
                break

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
        for target, original in getattr(self, "_patched", []):
            target.create = original
        self._patched = []
        if getattr(self, "_original_protect", None) is not None and Protect is not None:
            Protect.protect = self._original_protect
