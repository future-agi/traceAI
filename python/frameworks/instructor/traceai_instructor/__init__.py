from importlib import import_module
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
from wrapt import wrap_function_wrapper

_instruments = ("instructor >= 1.0.0",)


class InstructorInstrumentor(BaseInstrumentor):  # type: ignore
    __slots__ = (
        "_tracer",
        "_original_create",
        "_original_async_create",
        "_original_patch",
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
        client_module = import_module("instructor.core.client")
        self._original_create = getattr(client_module.Instructor, "create", None)
        self._original_async_create = getattr(client_module.AsyncInstructor, "create", None)
        wrap_function_wrapper(
            "instructor.core.client", "Instructor.create", create_wrapper
        )
        wrap_function_wrapper(
            "instructor.core.client", "AsyncInstructor.create", create_wrapper
        )

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
        if self._original_patch is not None:
            instructor_module = import_module("instructor")
            instructor_module.patch = self._original_patch  # type: ignore[attr-defined]
            self._original_patch = None

        client_module = import_module("instructor.core.client")
        if self._original_create is not None:
            client_module.Instructor.create = self._original_create
            self._original_create = None
        if self._original_async_create is not None:
            client_module.AsyncInstructor.create = self._original_async_create
            self._original_async_create = None
