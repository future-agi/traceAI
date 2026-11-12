import logging
from importlib import import_module
from typing import Any, Collection
logger = logging.getLogger(__name__)
try:
    from fi.evals import Protect
except ImportError:
    logger.warning("ai-evaluation is not installed, please install it to trace protect")
    Protect = None
    pass
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from groq.resources.chat.completions import AsyncCompletions, Completions
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from traceai_groq._wrappers import _AsyncCompletionsWrapper, _CompletionsWrapper
from traceai_groq.version import __version__
from wrapt import wrap_function_wrapper

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("groq >= 0.9.0",)


def _attachment_manager(kwargs: Any) -> Any:
    """The shared manager passed as ``attachments`` (or auto's ``attachment_manager``), if it can admit."""
    manager = kwargs.get("attachments")
    if manager is None:
        manager = kwargs.get("attachment_manager")
    if manager is None:
        return None
    try:
        from fi_instrumentation.attachments.media_capture import active
    except ImportError:
        return None
    return manager if active(manager) else None


class GroqInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """An instrumentor for the Groq framework."""

    __slots__ = (
        "_original_completions_create",
        "_original_async_completions_create",
        "_tracer",
        "_original_audio",
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

        self._original_completions_create = Completions.create
        wrap_function_wrapper(
            module="groq.resources.chat.completions",
            name="Completions.create",
            wrapper=_CompletionsWrapper(
                tracer=self._tracer, attachments=_attachment_manager(kwargs), config=config
            ),
        )

        self._original_async_completions_create = AsyncCompletions.create
        wrap_function_wrapper(
            module="groq.resources.chat.completions",
            name="AsyncCompletions.create",
            wrapper=_AsyncCompletionsWrapper(
                tracer=self._tracer, attachments=_attachment_manager(kwargs), config=config
            ),
        )

        # Audio spans exist only with an enabled attachment manager: plain tracing is unchanged.
        self._original_audio = []
        attachments = _attachment_manager(kwargs)
        if attachments is not None:
            from traceai_groq._audio import AUDIO_METHODS, wrapper_for

            for module_name, class_name, operation, is_async in AUDIO_METHODS:
                try:
                    owner = getattr(import_module(module_name), class_name)
                    original = owner.create
                except (ImportError, AttributeError):
                    continue  # Older SDKs have no such method (groq 0.9.0 has no speech).
                wrap_function_wrapper(
                    module=module_name,
                    name=f"{class_name}.create",
                    wrapper=wrapper_for(self._tracer, attachments, config, operation, is_async),
                )
                self._original_audio.append((owner, original))

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
        for owner, original in getattr(self, "_original_audio", None) or ():
            owner.create = original
        self._original_audio = []
        groq_module = import_module("groq.resources.chat.completions")
        if self._original_completions_create is not None:
            groq_module.Completions.create = self._original_completions_create
        if self._original_async_completions_create is not None:
            groq_module.AsyncCompletions.create = (
                self._original_async_completions_create
            )
