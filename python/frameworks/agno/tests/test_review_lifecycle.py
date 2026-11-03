"""Adjacent lifecycle paths for the independent Agno review regressions."""

import asyncio

import pytest
from agno.agent import Agent
from agno.tools.function import Function, FunctionCall
from agno.workflow import Workflow
from agno.workflow.parallel import Parallel
from agno.workflow.step import Step
from agno.workflow.types import StepInput
from openinference.instrumentation.agno.utils import _AGNO_PARENT_NODE_CONTEXT_KEY
from opentelemetry import context, trace
from traceai_agno import configure_agno_tracing

from .test_recall import harness as harness


@pytest.mark.parametrize("kind", ["step", "parallel"])
def test_sync_container_exception_restores_context(harness, kind):
    provider, exporter, tracker, model, settings = harness

    def fail():
        if kind == "step":
            # Real Step validation raises before executor invocation.
            Step(
                name="InvalidStep", agent=Agent(model=model(), telemetry=False)
            ).execute(None)
        else:
            # Invalid child is rejected by real Parallel preparation.
            Parallel(object(), name="InvalidParallel").execute(StepInput(input="test"))

    with pytest.raises(Exception) as baseline:
        fail()
    configure_agno_tracing(tracer_provider=provider)
    before = context.get_current()
    token = context.attach(before)
    try:
        with pytest.raises(type(baseline.value)):
            fail()
        restored = context.get_current()
        Agent(name="Independent", model=model(), telemetry=False).run("test")
        assert restored == before
        independent = next(
            s for s in exporter.get_finished_spans() if s.name == "Independent.run"
        )
        assert independent.parent is None
        assert len(tracker.started) == len(exporter.get_finished_spans())
    finally:
        context.detach(token)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.asyncio
async def test_async_semantic_graph_matches_span_parents(harness, parallel, streaming):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    children = [Agent(name="Left", model=model(), telemetry=False)]
    if parallel:
        children.append(Agent(name="Right", model=model(), telemetry=False))
        steps = [Parallel(*children, name="Branches")]
    else:
        steps = children
    wf = Workflow(name="Graph", steps=steps, telemetry=False)
    before = context.get_current()
    if streaming:
        async for _ in wf.arun("test", stream=True):
            assert context.get_current() == before
        # Agno stops consuming child async generators at its terminal event;
        # allow their scheduled finalizers to run before inspecting the export.
        for _ in range(50):
            await asyncio.sleep(0)
    else:
        await wf.arun("test")
    assert context.get_current() == before
    spans = exporter.get_finished_spans()
    by_span_id = {s.context.span_id: s for s in spans}
    # Every node with a node parent should point to its actual OTel parent,
    # including the parallel -> step -> agent edges in both concurrent branches.
    checked = 0
    for span in spans:
        parent_node = span.attributes.get("graph.node.parent_id")
        if parent_node:
            assert span.parent is not None
            parent = by_span_id[span.parent.span_id]
            assert parent_node == parent.attributes["graph.node.id"], span.name
            checked += 1
    assert checked >= (5 if parallel else 2), [
        (s.name, dict(s.attributes)) for s in spans
    ]
    assert len(tracker.started) == len(spans)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_successful_workflows_are_ok(harness, asynchronous, streaming):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    wf = Workflow(
        name="Success", steps=[Agent(model=model(), telemetry=False)], telemetry=False
    )
    if asynchronous:
        result = (
            [r async for r in wf.arun("test", stream=True)]
            if streaming
            else await wf.arun("test")
        )
    else:
        result = list(wf.run("test", stream=True)) if streaming else wf.run("test")
    assert result
    span = next(
        s for s in exporter.get_finished_spans() if s.name.startswith("Success.")
    )
    assert span.status.status_code == trace.StatusCode.OK


@pytest.mark.parametrize("mode", ["sync", "async-sync", "async"])
@pytest.mark.asyncio
async def test_unstarted_tool_close_is_idempotent_and_does_not_execute(harness, mode):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    calls = []

    def pieces():
        calls.append("executed")
        yield "first"

    async def apieces():
        calls.append("executed")
        yield "first"

    fc = FunctionCall(
        function=Function.from_callable(apieces if mode == "async" else pieces),
        arguments={},
    )
    before = context.get_current()
    response = fc.execute() if mode == "sync" else await fc.aexecute()
    assert len(tracker.started) == 1
    if mode == "async":
        await response.result.aclose()
        await response.result.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(response.result)
    else:
        response.result.close()
        response.result.close()
        with pytest.raises(StopIteration):
            next(response.result)
    assert calls == []
    assert context.get_current() == before
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code != trace.StatusCode.OK


@pytest.mark.asyncio
async def test_async_workflow_task_cancellation_restores_context(harness):
    provider, exporter, tracker, model, settings = harness
    entered = asyncio.Event()

    async def steps(workflow, execution_input, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    configure_agno_tracing(tracer_provider=provider)
    wf = Workflow(name="TaskCancelled", steps=steps, telemetry=False)
    before = context.get_current()
    task = asyncio.create_task(wf.arun("test"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.get_current() == before
    assert context.get_value(_AGNO_PARENT_NODE_CONTEXT_KEY) is None
    spans = exporter.get_finished_spans()
    assert len(spans) == len(tracker.started) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


@pytest.mark.parametrize(
    "terminal", ["completed", "cancelled", "error", "paused", "missing"]
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_workflow_terminal_event_status_without_run_output(
    harness, terminal, asynchronous
):
    from agno.run.workflow import (
        WorkflowCancelledEvent,
        WorkflowCompletedEvent,
        WorkflowErrorEvent,
        WorkflowPausedEvent,
    )
    from traceai_agno._wrappers._workflow_wrapper import _WorkflowWrapper

    provider, exporter, tracker, model, settings = harness
    events = {
        "completed": WorkflowCompletedEvent(),
        "cancelled": WorkflowCancelledEvent(),
        "error": WorkflowErrorEvent(),
        "paused": WorkflowPausedEvent(),
    }
    event = events.get(terminal)
    wrapper = _WorkflowWrapper(provider.get_tracer("test"))
    span = provider.get_tracer("test").start_span("Terminal")
    if asynchronous:

        async def stream():
            if event is not None:
                yield event

        assert [
            r async for r in wrapper._arun_stream_continue(stream(), span, "node")
        ] == ([] if event is None else [event])
    else:
        assert list(
            wrapper._run_stream_continue(
                iter([] if event is None else [event]), span, "node"
            )
        ) == ([] if event is None else [event])
    expected = {
        "completed": trace.StatusCode.OK,
        "cancelled": trace.StatusCode.ERROR,
        "error": trace.StatusCode.ERROR,
    }.get(terminal, trace.StatusCode.UNSET)
    assert exporter.get_finished_spans()[0].status.status_code == expected


@pytest.mark.parametrize("kind", ["workflow", "step", "parallel"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_unstarted_workflow_stream_close(harness, kind, asynchronous):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    agent = Agent(name="Child", model=model(), telemetry=False)
    before = context.get_current()
    if kind == "workflow":
        obj = Workflow(name="Unstarted", steps=[agent], telemetry=False)
        stream = (
            obj.arun("test", stream=True)
            if asynchronous
            else obj.run("test", stream=True)
        )
    else:
        obj = (
            Step(name="Unstarted", agent=agent)
            if kind == "step"
            else Parallel(agent, name="Unstarted")
        )
        stream = (
            obj.aexecute_stream(StepInput(input="test"))
            if asynchronous
            else obj.execute_stream(StepInput(input="test"))
        )
    assert len(tracker.started) == 1
    if asynchronous:
        await stream.aclose()
        await stream.aclose()
    else:
        stream.close()
        stream.close()
    assert context.get_current() == before
    assert len(exporter.get_finished_spans()) == len(tracker.started) == 1
    assert exporter.get_finished_spans()[0].status.status_code == trace.StatusCode.ERROR
