from importlib import import_module
from inspect import getattr_static
from typing import Any, Collection
import logging
logger = logging.getLogger(__name__)
try:
    from fi.evals import Protect
except ImportError:
    logger.warning("ai-evaluation is not installed, please install it to trace protect")
    Protect = None
    pass
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from traceai_instructor._wrappers import _HandleResponseWrapper, _PatchWrapper
from traceai_instructor.version import __version__
from wrapt import FunctionWrapper, wrap_function_wrapper

_instruments = ("instructor >= 1.0.0",)


class _OwnedProtectWrapper(GuardrailProtectWrapper):
    """A later ordinary decorator can close over our hook permanently."""

    def __init__(self, tracer):
        super().__init__(tracer)
        self.enabled = True

    def __call__(self, wrapped, instance, args, kwargs):
        if not self.enabled:
            return wrapped(*args, **kwargs)
        return super().__call__(wrapped, instance, args, kwargs)


class InstructorInstrumentor(BaseInstrumentor):  # type: ignore
    __slots__ = (
        "_tracer",
        "_original_create",
        "_original_async_create",
        "_original_patch",
        "_instructor_class",
        "_async_instructor_class",
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

        self._original_patch = getattr(import_module("instructor"), "patch", None)
        patch_wrapper = _PatchWrapper(tracer=self._tracer)
        wrap_function_wrapper("instructor", "patch", patch_wrapper)

        # Wrap Instructor.create and AsyncInstructor.create — these are the
        # actual entry points for structured-output LLM calls in instructor 1.x+.
        # (The old instructor.patch.handle_response_model was removed in 1.0.)
        create_wrapper = _HandleResponseWrapper(tracer=self._tracer)
        client_module = import_module("instructor")
        self._instructor_class = client_module.Instructor
        self._async_instructor_class = getattr(client_module, "AsyncInstructor", None)
        if self._async_instructor_class is None:
            # Instructor 1.0 exposes the async class only in instructor.client.
            self._async_instructor_class = import_module("instructor.client").AsyncInstructor
        self._original_create = self._instructor_class.create
        self._original_async_create = self._async_instructor_class.create
        wrap_function_wrapper(self._instructor_class, "create", create_wrapper)
        wrap_function_wrapper(self._async_instructor_class, "create", create_wrapper)

        self._protect_owner = Protect
        self._protect_hook = None
        if Protect is not None:
            self._protect_callback = _OwnedProtectWrapper(tracer=self._tracer)
            self._protect_hook = FunctionWrapper(
                getattr_static(Protect, "protect"),
                self._protect_callback,
            )
            Protect.protect = self._protect_hook

    def _uninstrument(self, **kwargs: Any) -> None:
        if self._original_patch is not None:
            instructor_module = import_module("instructor")
            instructor_module.patch = self._original_patch  # type: ignore[attr-defined]
            self._original_patch = None

        if self._original_create is not None:
            self._instructor_class.create = self._original_create
            self._original_create = None
        if self._original_async_create is not None:
            self._async_instructor_class.create = self._original_async_create
            self._original_async_create = None

        if self._protect_hook is not None:
            # Disable first: ordinary decorators close over the callable, so
            # mutating their __wrapped__ metadata cannot actually detach us.
            self._protect_callback.enabled = False
            # Protect is shared by several integrations. Splice just our exact
            # wrapper where the parent is a mutable wrapt dispatch link.
            owner, hook = self._protect_owner, self._protect_hook
            current = getattr_static(owner, "protect")
            if current is hook:
                owner.protect = hook.__wrapped__
            else:
                seen = set()
                while hasattr(current, "__wrapped__") and id(current) not in seen:
                    seen.add(id(current))
                    if current.__wrapped__ is hook:
                        if isinstance(current, FunctionWrapper):
                            current.__wrapped__ = hook.__wrapped__
                        break
                    current = current.__wrapped__
            self._protect_hook = None
            self._protect_owner = None
