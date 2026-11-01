from typing import Any, Callable, Collection, Optional

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from traceai_smolagents._wrappers import (
    _ModelWrapper,
    _RunWrapper,
    _StepWrapper,
    _ToolCallWrapper,
    _TimeoutWrapper,
)
from traceai_smolagents.version import __version__
from wrapt import wrap_function_wrapper

_instruments = ("smolagents >= 1.2.2",)


class SmolagentsInstrumentor(BaseInstrumentor):
    __slots__ = (
        "_original_run_method",
        "_original_step_methods",
        "_original_tool_call_method",
        "_original_model_call_methods",
        "_tracer",
        "_original_timeout",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        import smolagents
        from smolagents import CodeAgent, MultiStepAgent, Tool, ToolCallingAgent, models

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

        run_wrapper = _RunWrapper(tracer=self._tracer)
        self._original_run_method = getattr(MultiStepAgent, "run", None)
        wrap_function_wrapper(
            module="smolagents",
            name="MultiStepAgent.run",
            wrapper=run_wrapper,
        )

        # Patch the actual execution entry points, not inherited aliases.
        # Since 1.20 agents call generate/_step_stream; __call__/step delegate.
        self._original_step_methods = {}
        for step_cls in (CodeAgent, ToolCallingAgent):
            method = "_step_stream" if hasattr(step_cls, "_step_stream") else "step"
            self._original_step_methods[(step_cls, method)] = vars(step_cls).get(method)
            wrap_function_wrapper(step_cls, method, _StepWrapper(tracer=self._tracer))

        self._original_model_call_methods = {}
        exported_model_subclasses = {
            attr for attr in vars(smolagents).values()
            if isinstance(attr, type) and issubclass(attr, models.Model)
        }
        for model_subclass in exported_model_subclasses:
            method = "generate" if hasattr(model_subclass, "generate") else "__call__"
            # Wrapping inherited methods stacks spans and leaves wrapped aliases
            # behind on uninstrument. The defining class is already in this set.
            if method not in vars(model_subclass):
                continue
            self._original_model_call_methods[(model_subclass, method)] = vars(model_subclass)[method]
            wrap_function_wrapper(model_subclass, method, _ModelWrapper(tracer=self._tracer, method_name=method))

        tool_call_wrapper = _ToolCallWrapper(tracer=self._tracer)
        self._original_tool_call_method = getattr(Tool, "__call__", None)
        wrap_function_wrapper(
            module="smolagents",
            name="Tool.__call__",
            wrapper=tool_call_wrapper,
        )

        from smolagents import local_python_executor
        self._original_timeout = getattr(local_python_executor, "timeout", None)
        if self._original_timeout is not None:
            wrap_function_wrapper(local_python_executor, "timeout", _TimeoutWrapper())

    def _uninstrument(self, **kwargs: Any) -> None:
        from smolagents import MultiStepAgent, Tool

        if getattr(self, "_original_timeout", None) is not None:
            from smolagents import local_python_executor
            local_python_executor.timeout = self._original_timeout
            self._original_timeout = None

        if self._original_run_method is not None:
            MultiStepAgent.run = self._original_run_method
            self._original_run_method = None

        for originals in (self._original_step_methods, self._original_model_call_methods):
            if originals is not None:
                for (cls, method), original in originals.items():
                    if original is None:
                        delattr(cls, method)
                    else:
                        setattr(cls, method, original)
        self._original_step_methods = None
        self._original_model_call_methods = None

        if self._original_tool_call_method is not None:
            Tool.__call__ = self._original_tool_call_method
            self._original_tool_call_method = None
