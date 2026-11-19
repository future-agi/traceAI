import logging
from importlib import import_module
from importlib import metadata
from typing import Any, Collection

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

try:
    from fi.evals import Protect
except ImportError:
    Protect = None

_instruments = ("together >= 1.0.0",)

# Together 2+ uses Stainless resource names; 1.x uses the legacy names.
# Resolve the resource classes, not instance attributes on Together/AsyncTogether.
_RESOURCES = (
    ("together.resources.chat.completions", "CompletionsResource", "ChatCompletions", _ChatCompletionsWrapper),
    ("together.resources.chat.completions", "AsyncCompletionsResource", "AsyncChatCompletions", _AsyncChatCompletionsWrapper),
    ("together.resources.completions", "CompletionsResource", "Completions", _CompletionsWrapper),
    ("together.resources.completions", "AsyncCompletionsResource", "AsyncCompletions", _AsyncCompletionsWrapper),
    ("together.resources.embeddings", "EmbeddingsResource", "Embeddings", _EmbeddingsWrapper),
    ("together.resources.embeddings", "AsyncEmbeddingsResource", "AsyncEmbeddings", _AsyncEmbeddingsWrapper),
)


def _together_version() -> str:
    try:
        return metadata.version("together")
    except metadata.PackageNotFoundError:
        return "unknown"


class TogetherInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for Together AI 1.x, 2.x and 3.x clients."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def instrument(self, **kwargs: Any) -> Any:
        result = super().instrument(**kwargs)
        # BaseInstrumentor marks success whenever _instrument returns.
        if self._is_instrumented_by_opentelemetry and not self._originals:
            self._is_instrumented_by_opentelemetry = False
        return result

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        self._originals = []

        # Resolve every surface before patching. Missing surfaces are warnings,
        # and zero surfaces leave the instrumentor inactive (see instrument()).
        targets = []
        missing = []
        for module_name, current_name, legacy_name, wrapper in _RESOURCES:
            try:
                module = import_module(module_name)
            except ImportError:
                module = None
            owner = getattr(module, current_name, None) or getattr(module, legacy_name, None)
            if owner is None or not callable(vars(owner).get("create")):
                missing.append(f"{module_name}.{current_name}/{legacy_name}.create")
                continue
            targets.append((module_name, owner, "create", wrapper(tracer=self._tracer)))
        if missing:
            logger.warning(
                "Together SDK %s is not fully supported; these calls will not be traced: %s",
                _together_version(),
                ", ".join(missing),
            )
        if not targets:
            logger.error("Together instrumentation is not enabled because no supported SDK resources were found")
            return
        if Protect is not None:
            targets.append(("fi.evals", Protect, "protect", GuardrailProtectWrapper(tracer=self._tracer)))

        try:
            for module_name, owner, name, wrapper in targets:
                original = getattr(owner, name)
                wrap_function_wrapper(module_name, f"{owner.__name__}.{name}", wrapper)
                self._originals.append((owner, name, original, vars(owner)[name]))
        except Exception:
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        """Restore only methods still owned by this instrumentor, including Protect."""
        for owner, name, original, installed in reversed(self._originals):
            if vars(owner).get(name) is installed:
                setattr(owner, name, original)
        self._originals.clear()
