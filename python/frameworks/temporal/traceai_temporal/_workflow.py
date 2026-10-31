"""Deterministic workflow half. SDK span/exporter calls run ONLY via an extern.

IDs are derived from the run ID, not workflow.random(): instrumentation must not
consume the application's random sequence. Replays rebuild exactly the same parent
identity, including when an unfinished workflow moves to a different worker.
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from temporalio import workflow
from temporalio.exceptions import FailureError
from temporalio.worker import WorkflowInboundInterceptor, WorkflowOutboundInterceptor

from ._content import content_attributes

with workflow.unsafe.imports_passed_through():
    from opentelemetry import context, trace
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

    from ._propagation import extract, inject

EXTERN = "__traceai_temporal_complete_workflow"
_NO_RESULT = object()


def _workflow_context(info: Any) -> Any:
    parent = extract(info.headers)
    parent_sc = trace.get_current_span(parent).get_span_context()
    digest = hashlib.sha256(("traceai.temporal:" + info.run_id).encode()).digest()
    span_id = int.from_bytes(digest[:8], "big") or 1
    trace_id = parent_sc.trace_id if parent_sc.is_valid else (int.from_bytes(digest[8:24], "big") or 1)
    sc = trace.SpanContext(
        trace_id, span_id, is_remote=False,
        trace_flags=parent_sc.trace_flags if parent_sc.is_valid else trace.TraceFlags(1),
    )
    return parent, trace.set_span_in_context(trace.NonRecordingSpan(sc), parent)


class TemporalWorkflowInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        # Workflow-local deterministic state, rebuilt on replay; not a host cache.
        # A signal can terminate the run while execute_workflow is still waiting.
        self._completed = False
        self._input_attributes = {}
        super().init(_WorkflowOutbound(outbound))

    @staticmethod
    def _is_terminal(exc: BaseException) -> bool:
        if isinstance(exc, (asyncio.CancelledError, workflow.ContinueAsNewError)):
            return True
        # Use the SDK's classification, including native timeouts and configured
        # failure types. Old SDKs without this API treat Python timeouts as task
        # failures, so only their standard FailureError is safe to classify here.
        classify = getattr(workflow, "is_failure_exception", None)
        return classify(exc) if classify is not None else isinstance(exc, FailureError)

    def _complete(self, error: Any = None, result: Any = _NO_RESULT) -> None:
        try:
            if self._completed:
                return
            self._completed = True
            info = workflow.info()
            parent, ctx = _workflow_context(info)
            if (workflow.unsafe.is_replaying()
                    or context.get_value(_SUPPRESS_INSTRUMENTATION_KEY, ctx)
                    or context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)):
                return
            sc = trace.get_current_span(ctx).get_span_context()
            # Only plain immutable data crosses the sandbox boundary.
            delta = info.start_time - datetime(1970, 1, 1, tzinfo=timezone.utc)
            start_ns = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
            workflow.extern_functions()[EXTERN]({
                "parent": dict(inject({}, parent)),
                "trace_id": sc.trace_id,
                "span_id": sc.span_id,
                "name": "temporal.workflow " + info.workflow_type,
                "start_ns": start_ns,
                "end_ns": workflow.time_ns(),
                "attributes": {
                    "gen_ai.span.kind": "AGENT",
                    "traceai.capture.layer": "library",
                    "temporal.workflow.id": info.workflow_id,
                    "temporal.workflow.run_id": info.run_id,
                    "temporal.workflow.type": info.workflow_type,
                    "temporal.task_queue": info.task_queue,
                    **self._input_attributes,
                    **(content_attributes("output", result) if result is not _NO_RESULT else {}),
                },
                "error": error,
            })
        except Exception:
            # Telemetry must never turn workflow success into task failure.
            workflow.logger.debug("traceAI: Temporal workflow completion failed", exc_info=True)

    def _complete_on_error(self, exc: BaseException) -> None:
        try:
            # Ordinary exceptions fail a workflow TASK, not the workflow. Do not
            # export a false completion on task retry, eviction or shutdown.
            if self._is_terminal(exc):
                self._complete(None if isinstance(exc, workflow.ContinueAsNewError) else type(exc).__name__)
        except Exception:
            workflow.logger.debug("traceAI: Temporal failure classification failed", exc_info=True)

    async def execute_workflow(self, input: Any) -> Any:
        _, ctx = _workflow_context(workflow.info())
        # An outer workflow interceptor can suppress locally even when the
        # incoming header is unsuppressed. Context replacement must not undo it.
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            ctx = context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True, ctx)
        token = context.attach(ctx)
        # Snapshot before application code can mutate its arguments. Rebuild on
        # replay too: a replayed start can subsequently complete on a live task.
        # Pure builtin serialization neither calls converters nor changes history.
        try:
            self._input_attributes = content_attributes("input", input.args) if input is not None else {}
        except Exception:
            workflow.logger.debug("traceAI: Temporal workflow input capture failed")
        try:
            result = await super().execute_workflow(input)
        except BaseException as exc:
            self._complete_on_error(exc)
            raise
        else:
            self._complete(result=result)
            return result
        finally:
            context.detach(token)

    async def handle_signal(self, input: Any) -> None:
        try:
            return await super().handle_signal(input)
        except BaseException as exc:
            # Signal failures terminate outside execute_workflow. Emit the same
            # workflow identity, not a signal span, without changing the failure.
            self._complete_on_error(exc)
            raise


class _WorkflowOutbound(WorkflowOutboundInterceptor):
    @staticmethod
    def _propagate(input: Any) -> None:
        # Signal/update handlers use separate asyncio contexts. Derive the same
        # workflow parent even when this handler did not inherit the run task.
        _, ctx = _workflow_context(workflow.info())
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            ctx = context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True, ctx)
        input.headers = inject(input.headers, ctx)

    def start_activity(self, input: Any) -> Any:
        self._propagate(input)
        return super().start_activity(input)

    def start_local_activity(self, input: Any) -> Any:
        self._propagate(input)
        return super().start_local_activity(input)

    async def start_child_workflow(self, input: Any) -> Any:
        self._propagate(input)
        return await super().start_child_workflow(input)

    def continue_as_new(self, input: Any) -> Any:
        self._propagate(input)
        return super().continue_as_new(input)
