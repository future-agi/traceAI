"""Temporal's public client/worker interceptor API; no workflow monkey-patches."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace.id_generator import IdGenerator
from temporalio import activity, client, worker, workflow

from ._content import content_attributes
from ._propagation import extract, inject
from ._workflow import EXTERN, TemporalWorkflowInterceptor

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Activity chains are initialized synchronously after ALL worker plugins ran.
# Share selection only during that init traversal, never between activity tasks
# or via application-owned interceptor objects. This also covers tracing inherited
# from a client plus explicitly configured tracing added by a worker plugin.
_activity_selection: ContextVar = ContextVar("traceai_temporal_activity_selection", default=None)


class _WorkflowIDs(IdGenerator):
    def __init__(self, trace_id: int, span_id: int) -> None:
        self.trace_id, self.span_id = trace_id, span_id

    def generate_trace_id(self) -> int:
        return self.trace_id

    def generate_span_id(self) -> int:
        return self.span_id


class _WorkflowTracer(FITracer):
    """Use FITracer's supported ID-generator hook without changing the provider.

    This short-lived tracer is used outside the sandbox for one completed span.
    No global ID generator, live span or process-local workflow cache is mutated.
    """

    def __init__(self, wrapped: Any, config: TraceConfig, ids: IdGenerator) -> None:
        super().__init__(wrapped, config=config)
        self._self_workflow_ids = ids

    @property
    def id_generator(self) -> IdGenerator:
        return self._self_workflow_ids


def _finish(span: Any, error: Any = None, end_time: Any = None) -> None:
    if span is None:
        return
    try:
        if error:
            # Do not serialize exception messages/stacks or arbitrary user objects.
            span.set_attribute("error.type", error)
            span.set_status(trace.Status(trace.StatusCode.ERROR, error))
        else:
            span.set_status(trace.Status(trace.StatusCode.OK))
    except Exception:
        logger.debug("traceAI: Temporal span status failed", exc_info=True)
    finally:
        try:
            span.end(end_time=end_time)
        except Exception:
            logger.debug("traceAI: Temporal span end failed", exc_info=True)


def _set_content(span: Any, side: str, value: Any) -> None:
    try:
        if span is not None and span.is_recording():
            # FITracer masks input.* / output.* through its TraceConfig. The
            # agent's S1 export sanitizer remains the final privacy boundary.
            span.set_attributes(content_attributes(side, value))
    except Exception:
        logger.debug("traceAI: Temporal content capture failed", exc_info=True)


class TemporalInterceptor(client.Interceptor, worker.Interceptor):
    """Install on a Client OR Worker (client worker-interceptors are inherited).

    Workflow spans are emitted on completion, outside the sandbox, with stable
    identities. Activities are live TOOL spans, so SDK/wire LLM spans nest below.
    """

    def __init__(self, tracer_provider: Any = None, config: Any = None) -> None:
        self.config = config if config is not None else TraceConfig()
        self._automatic = False
        self._tracer_provider = tracer_provider
        self._otel_tracer = trace.get_tracer("traceai_temporal", "0.1.0", tracer_provider)
        self.tracer = FITracer(self._otel_tracer, config=self.config)

    def intercept_client(self, next: Any) -> Any:
        return _ClientOutbound(next, self)

    def intercept_activity(self, next: Any) -> Any:
        return _ActivityInbound(next, self)

    def workflow_interceptor_class(self, input: Any) -> Any:
        # The SDK visits the final merged client/worker interceptor sequence.
        # Install one sandbox class; its extern callback selects the first
        # explicit configuration, overriding an earlier automatic default.
        previous = input.unsafe_extern_functions.get(EXTERN)
        owner = getattr(previous, "__self__", None)
        if isinstance(owner, TemporalInterceptor):
            if owner._automatic and not self._automatic:
                input.unsafe_extern_functions[EXTERN] = self._complete_workflow
            return None
        input.unsafe_extern_functions[EXTERN] = self._complete_workflow
        return TemporalWorkflowInterceptor

    @contextmanager
    def _span(self, name: str, kind: Any, attributes: dict, headers: Any = None):
        span = token = None
        ctx = extract(headers) if headers is not None else context.get_current()
        try:
            # A worker interceptor may suppress locally even when the remote
            # workflow header is unsuppressed. Never clear that privacy boundary
            # when attaching the extracted parent for nested model/wire spans.
            if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                ctx = context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True, ctx)
            if not context.get_value(_SUPPRESS_INSTRUMENTATION_KEY, ctx):
                span = self.tracer.start_span(name, context=ctx, kind=kind, attributes=attributes)
                ctx = trace.set_span_in_context(span, ctx)
            token = context.attach(ctx)
        except Exception:
            logger.debug("traceAI: Temporal span setup failed", exc_info=True)
        try:
            yield span
        except BaseException as exc:
            if isinstance(exc, getattr(activity, "_CompleteAsyncError", ())):
                # Temporal consumes this control-flow exception and completes the
                # activity via an external async handle. Successful handoff is not
                # an activity failure; its eventual result is not available here.
                try:
                    if span is not None:
                        span.set_attribute("temporal.activity.async_completion", True)
                except Exception:
                    logger.debug("traceAI: Temporal async handoff marker failed", exc_info=True)
                _finish(span)
            else:
                _finish(span, type(exc).__name__)
            raise
        else:
            _finish(span)
        finally:
            if token is not None:
                try:
                    context.detach(token)
                except Exception:
                    logger.debug("traceAI: Temporal context detach failed", exc_info=True)

    def _complete_workflow(self, data: dict) -> None:
        try:
            # Extern callbacks retain sandbox import/builtin hooks. Only this
            # host-side telemetry callback may perform exporter I/O or lazy
            # imports; the workflow itself always remains sandboxed.
            with workflow.unsafe.imports_passed_through(), workflow.unsafe.sandbox_unrestricted():
                tracer = _WorkflowTracer(
                    trace.get_tracer("traceai_temporal", "0.1.0", self._tracer_provider), self.config,
                    _WorkflowIDs(data["trace_id"], data["span_id"]),
                )
                span = tracer.start_span(
                    data["name"], context=extract(data["parent"]),
                    kind=trace.SpanKind.SERVER, attributes=data["attributes"],
                    start_time=data["start_ns"],
                )
                _finish(span, data["error"], data["end_ns"])
        except Exception:
            # Never cross the sandbox boundary with an instrumentation exception.
            logger.debug("traceAI: Temporal workflow export failed", exc_info=True)


class _ClientOutbound(client.OutboundInterceptor):
    def __init__(self, next: Any, root: TemporalInterceptor) -> None:
        super().__init__(next)
        self.root = root

    @contextmanager
    def _start(self, input: Any):
        with self.root._span(
            "temporal.start_workflow " + input.workflow, trace.SpanKind.CLIENT,
            {"gen_ai.span.kind": "CHAIN", "traceai.capture.layer": "library",
             "temporal.workflow.id": input.id, "temporal.workflow.type": input.workflow},
        ) as span:
            _set_content(span, "input", getattr(input, "args", ()))
            input.headers = inject(input.headers, context.get_current())
            yield

    async def start_workflow(self, input: Any) -> Any:
        with self._start(input):
            return await super().start_workflow(input)

    async def start_update_with_start_workflow(self, input: Any) -> Any:
        # Both execute_update_with_start_workflow and start_update_with_start_workflow
        # use this outbound hook instead of start_workflow. Only the start half
        # carries the workflow's execution parent; preserve the update unchanged.
        with self._start(input.start_workflow_input):
            return await super().start_update_with_start_workflow(input)


class _ActivityInbound(worker.ActivityInboundInterceptor):
    def __init__(self, next: Any, root: TemporalInterceptor) -> None:
        super().__init__(next)
        self.root = root
        self._selection = [self]

    def init(self, outbound: Any) -> None:
        selection = _activity_selection.get()
        token = None
        if selection is None:
            selection = [self]
            token = _activity_selection.set(selection)
        elif selection[0].root._automatic and not self.root._automatic:
            selection[0] = self
        self._selection = selection
        try:
            super().init(outbound)
        finally:
            if token is not None:
                _activity_selection.reset(token)

    async def execute_activity(self, input: Any) -> Any:
        if self._selection[0] is not self:
            return await super().execute_activity(input)
        info = activity.info()
        with self.root._span(
            "temporal.activity " + info.activity_type, trace.SpanKind.SERVER,
            {"gen_ai.span.kind": "TOOL", "traceai.capture.layer": "library",
             "temporal.workflow.id": info.workflow_id or "",
             "temporal.workflow.run_id": info.workflow_run_id or "",
             "temporal.activity.id": info.activity_id,
             "temporal.activity.type": info.activity_type,
             "temporal.activity.attempt": info.attempt,
             "temporal.activity.local": info.is_local},
            headers=input.headers,
        ) as span:
            _set_content(span, "input", input.args)
            result = await super().execute_activity(input)
            _set_content(span, "output", result)
            return result
