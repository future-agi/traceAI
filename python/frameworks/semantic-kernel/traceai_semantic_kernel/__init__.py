"""Future AGI tracing for Semantic Kernel (Python) on its native diagnostics.

Semantic Kernel already emits OpenTelemetry GenAI spans (``chat``,
``invoke_agent``, ``execute_tool``) once its experimental diagnostics are on.
:class:`SemanticKernelInstrumentor` turns those diagnostics on in process and
installs a mapping-only span processor. It does not wrap ``Kernel.invoke``,
``KernelFunction.invoke`` or any connector method, and it does not depend on
``wrapt``.

Experimental upstream surface, Python only. Not the Java package, not .NET
Semantic Kernel, not Microsoft Agent Framework.
"""

from __future__ import annotations

import importlib
import logging
import threading
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from opentelemetry import trace as trace_api

from .processor import (
    CONTENT_KEYS,
    FI_SPAN_KIND,
    KIND_BY_OPERATION,
    PROMOTED_USAGE_KEYS,
    SemanticKernelSpanProcessor,
    kind_for,
    map_sk_attributes,
)
from .version import __version__

logger = logging.getLogger(__name__)

_instruments = ("semantic-kernel >= 1.38.0",)

# Modules that each build their own ``ModelDiagnosticSettings()`` at import
# time (semantic-kernel 1.44.1: model_diagnostics/decorators.py:34,
# model_diagnostics/function_tracer.py:27, agent_diagnostics/decorators.py:28).
# Setting the environment variables after import would not change these
# instances, so instrument() assigns the fields on each instance instead.
DIAGNOSTICS_SETTINGS_MODULES: Tuple[str, ...] = (
    "semantic_kernel.utils.telemetry.model_diagnostics.decorators",
    "semantic_kernel.utils.telemetry.model_diagnostics.function_tracer",
    "semantic_kernel.utils.telemetry.agent_diagnostics.decorators",
)

# Modules whose spans come from a module-level ``tracer = get_tracer(__name__)``
# bound to the *global* tracer provider (model_diagnostics/decorators.py:65,
# agent_diagnostics/decorators.py:33, functions/kernel_function.py:51,
# connectors/ai/chat_completion_client_base.py:32). ``fi_instrumentation.register()``
# does not set the global provider by default, so instrument() points these
# names at a tracer from the provider you pass. No function is replaced.
TRACER_MODULES: Tuple[str, ...] = (
    "semantic_kernel.utils.telemetry.model_diagnostics.decorators",
    "semantic_kernel.utils.telemetry.agent_diagnostics.decorators",
    "semantic_kernel.functions.kernel_function",
    "semantic_kernel.connectors.ai.chat_completion_client_base",
)


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        return None


def _span_processor_chain(provider: Any) -> Any:
    """Return the provider's SDK multi span processor, or None for a non-SDK provider."""
    active = getattr(provider, "_active_span_processor", None)
    if active is None or not hasattr(active, "_span_processors"):
        return None
    return active


def _import_modules(names: Tuple[str, ...], cache: Dict[str, Optional[ModuleType]]) -> List[ModuleType]:
    """Import each module once; skip, with a WARNING naming it, any that cannot be imported."""
    modules: List[ModuleType] = []
    for name in names:
        if name not in cache:
            try:
                cache[name] = importlib.import_module(name)
            except Exception as exc:
                logger.warning(
                    "traceai-semantic-kernel: skipping %s, which could not be imported (%s: %s). This Semantic "
                    "Kernel release may have moved its experimental diagnostics; the rest stays instrumented.",
                    name,
                    type(exc).__name__,
                    exc,
                )
                cache[name] = None
        module = cache[name]
        if module is not None:
            modules.append(module)
    return modules


def _sk_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("semantic-kernel")
    except Exception:  # pragma: no cover - metadata missing in odd installs
        return None


class _State:
    """Process-wide record of what instrument() changed, so it can be undone."""

    def __init__(self) -> None:
        self.provider: Any = None
        self.processor: Optional[SemanticKernelSpanProcessor] = None
        self.sensitive = False
        self.settings: List[Tuple[Any, bool, bool]] = []
        self.tracers: List[Tuple[Any, Any]] = []


class SemanticKernelInstrumentor:
    """Enable Semantic Kernel's native OpenTelemetry diagnostics for Future AGI.

    ``instrument()`` is process-wide and idempotent: Semantic Kernel keeps its
    diagnostics switches and tracers at module level, so every instance of
    this class shares one installation.
    """

    _lock = threading.RLock()
    _state: Optional[_State] = None

    @property
    def is_instrumented(self) -> bool:
        return SemanticKernelInstrumentor._state is not None

    @property
    def processor(self) -> Optional[SemanticKernelSpanProcessor]:
        state = SemanticKernelInstrumentor._state
        return state.processor if state is not None else None

    def instrumentation_dependencies(self) -> Tuple[str, ...]:
        return _instruments

    def instrument(
        self,
        tracer_provider: Optional[Any] = None,
        *,
        sensitive: bool = False,
        **kwargs: Any,
    ) -> None:
        """Turn on Semantic Kernel diagnostics and install the mapping processor.

        Args:
            tracer_provider: The provider returned by ``fi_instrumentation.register``.
                Defaults to the global provider, which must then be an SDK provider.
            sensitive: Also turn on Semantic Kernel's *sensitive* diagnostics.
                Off by default. **Warning:** when True, Semantic Kernel puts
                agent input/output messages, tool-call arguments and tool
                results on spans, and this package copies them to
                ``input.value`` / ``output.value``. Leave it False unless you
                want that text stored in Future AGI.

        No environment variable is required. Calling this more than once (on
        any instance) installs one processor; later calls are no-ops until
        :meth:`uninstrument`.

        It does not raise into your startup for a Semantic Kernel module this
        package expects but cannot import (the diagnostics are experimental
        upstream and may move): that module is skipped with a WARNING naming
        it, and the rest is instrumented. A provider that is not an
        OpenTelemetry SDK ``TracerProvider`` gets a WARNING and nothing is
        changed.
        """
        if kwargs:
            logger.debug("traceai-semantic-kernel: ignoring unsupported instrument() arguments %s", sorted(kwargs))
        with SemanticKernelInstrumentor._lock:
            state = SemanticKernelInstrumentor._state
            if state is not None:
                if tracer_provider is not None and tracer_provider is not state.provider:
                    logger.warning(
                        "traceai-semantic-kernel is already instrumented on another tracer provider; "
                        "call uninstrument() first to switch providers."
                    )
                if bool(sensitive) != state.sensitive:
                    logger.warning(
                        "traceai-semantic-kernel is already instrumented with sensitive=%s; keeping it.",
                        state.sensitive,
                    )
                return

            provider = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()
            active = _span_processor_chain(provider)
            if active is None:
                logger.warning(
                    "traceai-semantic-kernel: not instrumenting. instrument() needs an OpenTelemetry SDK "
                    "TracerProvider, for example the one returned by fi_instrumentation.register(project_type="
                    "ProjectType.OBSERVE, project_name=...), passed as tracer_provider=. Got %s. Semantic Kernel "
                    "is left unchanged.",
                    type(provider).__name__,
                )
                return

            cache: Dict[str, Optional[ModuleType]] = {}
            settings_modules = _import_modules(DIAGNOSTICS_SETTINGS_MODULES, cache)
            tracer_modules = _import_modules(TRACER_MODULES, cache)

            new_state = _State()
            new_state.provider = provider
            new_state.sensitive = bool(sensitive)
            try:
                self._apply(new_state, active, settings_modules, tracer_modules, tracer_provider, sensitive)
            except Exception:
                # Undo whatever was applied, then surface the error at setup time.
                SemanticKernelInstrumentor._state = new_state
                self.uninstrument()
                raise
            SemanticKernelInstrumentor._state = new_state

    @staticmethod
    def _apply(
        new_state: _State,
        active: Any,
        settings_modules: List[Any],
        tracer_modules: List[Any],
        tracer_provider: Optional[Any],
        sensitive: bool,
    ) -> None:
        # 1. Processor first, ahead of the exporting processor. Not via
        # provider.add_span_processor(): fi_instrumentation's TracerProvider
        # drops its default exporting processor on the first call after
        # register() (fi_instrumentation/otel.py TracerProvider.add_span_processor).
        existing = [p for p in active._span_processors if isinstance(p, SemanticKernelSpanProcessor)]
        if existing:
            processor = existing[0]
        else:
            processor = SemanticKernelSpanProcessor(sensitive=sensitive)
            with getattr(active, "_lock", _NullLock()):
                active._span_processors = (processor,) + tuple(active._span_processors)
        new_state.processor = processor

        # 2. Diagnostics switches, in process.
        for module in settings_modules:
            settings = getattr(module, "MODEL_DIAGNOSTICS_SETTINGS", None)
            if settings is None:
                logger.warning(
                    "traceai-semantic-kernel: %s has no MODEL_DIAGNOSTICS_SETTINGS; skipping it", module.__name__
                )
                continue
            try:
                was_on = bool(settings.enable_otel_diagnostics)
                was_sensitive = bool(settings.enable_otel_diagnostics_sensitive)
            except AttributeError as exc:
                logger.warning("traceai-semantic-kernel: skipping %s: %s", module.__name__, exc)
                continue
            if was_sensitive and not sensitive:
                logger.warning(
                    "traceai-semantic-kernel: Semantic Kernel sensitive diagnostics were on (environment "
                    "or .env); instrument(sensitive=False) turns them off. Pass sensitive=True to keep them."
                )
            new_state.settings.append((settings, was_on, was_sensitive))
            settings.enable_otel_diagnostics = True
            settings.enable_otel_diagnostics_sensitive = bool(sensitive)

        # 3. Route Semantic Kernel's module-level tracers to this provider.
        if tracer_provider is not None:
            version = _sk_version()
            for module in tracer_modules:
                original = getattr(module, "tracer", None)
                if original is None:
                    logger.warning(
                        "traceai-semantic-kernel: %s has no module-level tracer; skipping it", module.__name__
                    )
                    continue
                new_state.tracers.append((module, original))
                module.tracer = tracer_provider.get_tracer(module.__name__, version)

    def uninstrument(self, **_kwargs: Any) -> None:
        """Restore Semantic Kernel's switches and tracers and remove the processor."""
        with SemanticKernelInstrumentor._lock:
            state = SemanticKernelInstrumentor._state
            if state is None:
                return
            for settings, was_on, was_sensitive in state.settings:
                settings.enable_otel_diagnostics = was_on
                settings.enable_otel_diagnostics_sensitive = was_sensitive
            for module, original in state.tracers:
                module.tracer = original
            active = getattr(state.provider, "_active_span_processor", None)
            if active is not None and hasattr(active, "_span_processors"):
                with getattr(active, "_lock", _NullLock()):
                    active._span_processors = tuple(
                        p for p in active._span_processors if p is not state.processor
                    )
            if state.processor is not None:
                state.processor.shutdown()
            SemanticKernelInstrumentor._state = None


__all__ = [
    "CONTENT_KEYS",
    "DIAGNOSTICS_SETTINGS_MODULES",
    "FI_SPAN_KIND",
    "KIND_BY_OPERATION",
    "PROMOTED_USAGE_KEYS",
    "SemanticKernelInstrumentor",
    "SemanticKernelSpanProcessor",
    "TRACER_MODULES",
    "kind_for",
    "map_sk_attributes",
    "__version__",
]
