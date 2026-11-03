# Adapted from OpenInference Agno 1.1.0 (Arize AI), Apache-2.0.
# Modified by Future AGI; see NOTICE and LICENSE in this directory.

from types import MethodType
from typing import Any, Callable

from openinference.instrumentation import OITracer, TraceConfig
from openinference.instrumentation.agno import AgnoInstrumentor, find_model_subclasses
from opentelemetry import trace
from wrapt import wrap_function_wrapper


def activate(delegate: AgnoInstrumentor, **kwargs: Any) -> None:
    """Keep upstream ownership/conflict checks and teardown, using our wrappers.

    Only the singleton's installation callback is substituted, under the caller's
    setup lock. It is restored even on failure. No upstream module/class is
    patched and no second set of Agno wrappers is installed.
    """
    original = delegate.__dict__.get("_instrument")
    delegate._instrument = MethodType(_instrument, delegate)  # type: ignore[method-assign]
    try:
        delegate.instrument(**kwargs)
    finally:
        if original is None:
            del delegate._instrument
        else:
            delegate._instrument = original  # type: ignore[method-assign]


def _wrap_methods(
    target: Any,
    wrappers: dict[str, Callable[..., Any]],
    originals: dict[str, Any],
) -> None:
    for name, wrapper in wrappers.items():
        original = getattr(target, name, None)
        if callable(original):
            originals[name] = original
            wrap_function_wrapper(target, name, wrapper)


def _instrument(self: Any, **kwargs: Any) -> None:
    from agno.agent import _run as agent_run
    from agno.team import _run as team_run
    from agno.tools.function import FunctionCall
    from agno.workflow.parallel import Parallel
    from agno.workflow.step import Step
    from agno.workflow.workflow import Workflow

    from traceai_agno import __version__

    from ._model_wrapper import _ModelWrapper
    from ._runs_wrapper import _RunWrapper
    from ._tools_wrapper import _FunctionCallWrapper
    from ._workflow_wrapper import (
        _ParallelWrapper,
        _StepWrapper,
        _WorkflowExecuteWrapper,
        _WorkflowWrapper,
    )

    config = kwargs.get("config") or TraceConfig()
    if not isinstance(config, TraceConfig):
        raise TypeError("config must be an OpenInference TraceConfig")
    self._tracer = OITracer(
        trace.get_tracer("traceai_agno", __version__, kwargs.get("tracer_provider")),
        config=config,
    )
    run = _RunWrapper(self._tracer)
    model = _ModelWrapper(self._tracer, hide_inputs=bool(config.hide_inputs))
    tool = _FunctionCallWrapper(self._tracer, hide_inputs=bool(config.hide_inputs))
    workflow = _WorkflowWrapper(self._tracer)
    execute = _WorkflowExecuteWrapper(self._tracer)
    step = _StepWrapper(self._tracer)
    parallel = _ParallelWrapper(self._tracer)

    # Match the pinned upstream teardown's storage contract. Initialize all
    # slots before installing anything so partial installation can be undone.
    for name in AgnoInstrumentor.__slots__:
        if name.startswith("_original_"):
            setattr(self, name, None)
    self._original_model_call_methods = {}
    self._original_workflow_methods = {}
    self._original_step_methods = {}
    self._original_parallel_methods = {}

    run_methods = {
        "run": run.run,
        "run_stream": run.run_stream,
        "arun": run.arun,
        "arun_stream": run.arun_stream,
        "continue_run": run.run,
        "continue_run_stream": run.run_stream,
        "acontinue_run": run.arun,
        "acontinue_run_stream": run.arun_stream,
    }
    try:
        for module, prefix in ((agent_run, ""), (team_run, "team_")):
            for name, wrapper in run_methods.items():
                original = getattr(module, f"_{name}", None)
                if callable(original):
                    setattr(self, f"_original_{prefix}{name}_method", original)
                    wrap_function_wrapper(module, f"_{name}", wrapper)

        # Upstream discovery orders subclasses before their bases, avoiding an
        # extra wrapper inherited from an already-instrumented base class.
        for cls in find_model_subclasses():
            originals: dict[str, Any] = {}
            self._original_model_call_methods[cls] = originals
            _wrap_methods(
                cls,
                {
                    "invoke": model.run,
                    "ainvoke": model.arun,
                    "invoke_stream": model.run_stream,
                    "ainvoke_stream": model.arun_stream,
                },
                originals,
            )

        for name, wrapper in (("execute", tool.run), ("aexecute", tool.arun)):
            setattr(
                self, f"_original_function_{name}_method", getattr(FunctionCall, name)
            )
            wrap_function_wrapper(FunctionCall, name, wrapper)

        _wrap_methods(
            Workflow,
            {
                "run": workflow.run,
                "arun": workflow.arun,
                "_aexecute": execute.aexecute,
                "_aexecute_stream": execute.aexecute_stream,
            },
            self._original_workflow_methods,
        )
        _wrap_methods(
            Step,
            {
                "execute": step.run,
                "execute_stream": step.run,
                "aexecute": step.arun,
                "aexecute_stream": step.arun,
            },
            self._original_step_methods,
        )
        _wrap_methods(
            Parallel,
            {
                "execute": parallel.execute,
                "execute_stream": parallel.execute,
                "aexecute": parallel.aexecute,
                "aexecute_stream": parallel.aexecute,
            },
            self._original_parallel_methods,
        )
    except BaseException:
        # BaseInstrumentor marks activation only after this callback returns;
        # its public uninstrument() would therefore skip rollback here.
        self._uninstrument()
        raise
