import logging
from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from traceai_semantic_kernel._wrappers import (
    _KernelInvokeWrapper,
    _FunctionInvokeWrapper,
)
from traceai_semantic_kernel.version import __version__
from wrapt import wrap_function_wrapper

_instruments = ("semantic-kernel >= 1.5.0",)

logger = logging.getLogger(__name__)


class SemanticKernelInstrumentor(BaseInstrumentor):  # type: ignore
    __slots__ = (
        "_original_kernel_invoke",
        "_original_function_invoke",
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

        try:
            # Wrap Kernel.invoke
            kernel_invoke_wrapper = _KernelInvokeWrapper(tracer=self._tracer)
            self._original_kernel_invoke = getattr(
                import_module("semantic_kernel.kernel"), "Kernel.invoke", None
            )
            wrap_function_wrapper(
                module="semantic_kernel.kernel",
                name="Kernel.invoke",
                wrapper=kernel_invoke_wrapper,
            )
        except Exception as e:
            logger.debug(f"Failed to instrument Kernel.invoke: {e}")
            self._original_kernel_invoke = None

        try:
            # Wrap KernelFunction.invoke
            function_invoke_wrapper = _FunctionInvokeWrapper(tracer=self._tracer)
            self._original_function_invoke = getattr(
                import_module("semantic_kernel.functions.kernel_function"), "KernelFunction.invoke", None
            )
            wrap_function_wrapper(
                module="semantic_kernel.functions.kernel_function",
                name="KernelFunction.invoke",
                wrapper=function_invoke_wrapper,
            )
        except Exception as e:
            logger.debug(f"Failed to instrument KernelFunction.invoke: {e}")
            self._original_function_invoke = None

    def _uninstrument(self, **kwargs: Any) -> None:
        if self._original_kernel_invoke is not None:
            kernel_module = import_module("semantic_kernel.kernel")
            kernel_module.Kernel.invoke = self._original_kernel_invoke
            self._original_kernel_invoke = None

        if self._original_function_invoke is not None:
            function_module = import_module("semantic_kernel.functions.kernel_function")
            function_module.KernelFunction.invoke = self._original_function_invoke
            self._original_function_invoke = None
