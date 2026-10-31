import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fi_instrumentation import suppress_tracing
from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import WorkflowOutboundInterceptor

from traceai_temporal._propagation import decode, inject
from traceai_temporal._workflow import EXTERN, TemporalWorkflowInterceptor, _WorkflowOutbound


@pytest.mark.parametrize("remote_suppressed,local_suppressed", [
    (False, False), (False, True), (True, False), (True, True),
])
async def test_workflow_context_preserves_local_and_remote_suppression(remote_suppressed, local_suppressed):
    from contextlib import nullcontext

    remote = context.Context()
    if remote_suppressed:
        remote = context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True, remote)
    info = SimpleNamespace(
        headers=inject({}, remote), run_id="suppression-run", workflow_id="workflow-id",
        workflow_type="Unit", task_queue="queue",
        start_time=datetime(2026, 10, 30, tzinfo=timezone.utc),
    )
    sentinel = object()
    expected = remote_suppressed or local_suppressed

    async def execute(input):
        assert bool(context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)) is expected
        outbound = SimpleNamespace(headers={})
        _WorkflowOutbound._propagate(outbound)
        assert (decode(outbound.headers) == {"disabled": True}) is expected
        return sentinel

    next = Mock()
    next.execute_workflow = AsyncMock(side_effect=execute)
    inbound = TemporalWorkflowInterceptor(next)
    inbound.init(WorkflowOutboundInterceptor(None))
    completions = []
    with (
        patch.object(workflow, "info", return_value=info),
        patch.object(workflow, "time_ns", return_value=123456),
        patch.object(workflow, "extern_functions", return_value={EXTERN: completions.append}),
        patch.object(workflow.unsafe, "is_replaying", return_value=False),
        patch.object(workflow, "is_failure_exception", create=True, return_value=False),
        patch.object(workflow, "logger"),
    ):
        with suppress_tracing() if local_suppressed else nullcontext():
            previous = context.get_current()
            assert await inbound.execute_workflow(None) is sentinel
            assert context.get_current() is previous
    assert len(completions) == int(not expected)
    assert not context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)


def test_terminal_classification_delegates_to_sdk_and_legacy_fallback():
    error = TimeoutError()
    with patch.object(workflow, "is_failure_exception", create=True, return_value=True) as classify:
        assert TemporalWorkflowInterceptor._is_terminal(error)
        classify.assert_called_once_with(error)
    with patch.object(workflow, "is_failure_exception", create=True, return_value=False):
        assert not TemporalWorkflowInterceptor._is_terminal(error)
    # Legacy SDKs don't fail workflows on Python timeouts by default.
    with patch.object(workflow, "is_failure_exception", create=True, new=None):
        assert not TemporalWorkflowInterceptor._is_terminal(error)
        assert TemporalWorkflowInterceptor._is_terminal(ApplicationError("private"))
    assert TemporalWorkflowInterceptor._is_terminal(asyncio.CancelledError())


@pytest.mark.parametrize("signal_terminal", [False, True])
async def test_signal_and_run_share_one_completion_and_preserve_exceptions(signal_terminal):
    next = Mock()
    next.execute_workflow = AsyncMock()
    next.handle_signal = AsyncMock()
    inbound = TemporalWorkflowInterceptor(next)
    inbound.init(WorkflowOutboundInterceptor(None))
    task_error = ValueError("task retry")
    signal_error = ApplicationError("private failure") if signal_terminal else task_error
    completions = []
    info = SimpleNamespace(
        headers={}, run_id="stable-run-id", workflow_id="workflow-id",
        workflow_type="Unit", task_queue="queue",
        start_time=datetime(2026, 10, 30, tzinfo=timezone.utc),
    )
    with (
        patch.object(workflow, "info", return_value=info),
        patch.object(workflow, "time_ns", return_value=123456),
        patch.object(workflow, "extern_functions", return_value={EXTERN: completions.append}),
        patch.object(workflow.unsafe, "is_replaying", return_value=False),
        patch.object(workflow, "is_failure_exception", create=True, side_effect=lambda exc: isinstance(exc, ApplicationError)),
    ):
        next.execute_workflow.side_effect = task_error
        with pytest.raises(ValueError) as caught:
            await inbound.execute_workflow(None)
        assert caught.value is task_error
        assert completions == []
        assert not inbound._completed

        next.handle_signal.side_effect = signal_error
        with pytest.raises(type(signal_error)) as caught:
            await inbound.handle_signal(None)
        assert caught.value is signal_error
        assert len(completions) == int(signal_terminal)

        if signal_terminal:
            cancellation = asyncio.CancelledError()
            next.execute_workflow.side_effect = cancellation
            with pytest.raises(asyncio.CancelledError) as caught:
                await inbound.execute_workflow(None)
            assert caught.value is cancellation
            assert completions[0]["error"] == "ApplicationError"
        else:
            next.handle_signal.side_effect = None
            await inbound.handle_signal(None)
            assert completions == []
            next.execute_workflow.side_effect = None
            sentinel = object()
            next.execute_workflow.return_value = sentinel
            assert await inbound.execute_workflow(None) is sentinel
            assert completions[0]["error"] is None
    assert len(completions) == 1
    assert completions[0]["name"] == "temporal.workflow Unit"


async def test_failure_classifier_error_does_not_replace_signal_exception():
    failure = ApplicationError("original")
    next = Mock()
    next.handle_signal = AsyncMock(side_effect=failure)
    inbound = TemporalWorkflowInterceptor(next)
    inbound.init(WorkflowOutboundInterceptor(None))
    with (
        patch.object(workflow, "is_failure_exception", create=True, side_effect=RuntimeError("telemetry")),
        patch.object(workflow, "logger"),
    ):
        with pytest.raises(ApplicationError) as caught:
            await inbound.handle_signal(None)
    assert caught.value is failure
    assert not inbound._completed
