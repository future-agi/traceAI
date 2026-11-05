"""The tracing generator must release the Agno workflow source on every exit."""

from types import SimpleNamespace

import pytest
from agno.run.workflow import WorkflowRunEvent
from agno.workflow import Workflow
from openinference.instrumentation import OITracer, TraceConfig
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_agno._wrappers._workflow_wrapper import (
    _WorkflowExecuteWrapper,
    _WorkflowWrapper,
)


@pytest.fixture
def workflow_tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = OITracer(provider.get_tracer("workflow-ownership"), config=TraceConfig())
    yield tracer, exporter
    provider.shutdown()


def response():
    return SimpleNamespace(event=WorkflowRunEvent.workflow_completed.value)


@pytest.mark.parametrize("exit_kind", ["close", "complete", "error"])
def test_sync_workflow_releases_source(workflow_tracing, exit_kind):
    tracer, exporter = workflow_tracing
    workflow = Workflow(name="Synthetic", steps=[])
    events = []

    def source():
        try:
            events.append("started")
            yield response()
            if exit_kind == "error":
                raise ValueError("source failed")
        finally:
            events.append("closed")

    underlying = source()
    stream = _WorkflowWrapper(tracer).run(lambda: underlying, workflow, (), {})
    next(stream)
    if exit_kind == "close":
        stream.close()
    elif exit_kind == "error":
        with pytest.raises(ValueError, match="source failed"):
            next(stream)
    else:
        with pytest.raises(StopIteration):
            next(stream)
    assert events == ["started", "closed"]
    assert underlying.gi_frame is None
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == (
        trace.StatusCode.ERROR if exit_kind == "error" else trace.StatusCode.OK
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["close", "complete", "error"])
async def test_async_workflow_releases_source(workflow_tracing, exit_kind):
    tracer, exporter = workflow_tracing
    workflow = Workflow(name="Synthetic", steps=[])
    events = []

    async def source():
        try:
            events.append("started")
            yield response()
            if exit_kind == "error":
                raise ValueError("source failed")
        finally:
            events.append("closed")

    underlying = source()
    stream = _WorkflowWrapper(tracer).arun(lambda: underlying, workflow, (), {})
    await anext(stream)
    if exit_kind == "close":
        await stream.aclose()
    elif exit_kind == "error":
        with pytest.raises(ValueError, match="source failed"):
            await anext(stream)
    else:
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert events == ["started", "closed"]
    assert underlying.ag_frame is None
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == (
        trace.StatusCode.ERROR if exit_kind == "error" else trace.StatusCode.OK
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["traced", "suppressed", "already_spanned"])
async def test_nested_workflow_stream_releases_source(
    workflow_tracing, mode, monkeypatch
):
    tracer, exporter = workflow_tracing
    workflow = Workflow(name="Synthetic", steps=[])
    wrapper = _WorkflowExecuteWrapper(tracer)
    events = []

    async def source():
        try:
            events.append("started")
            yield "partial"
        finally:
            events.append("closed")

    underlying = source()
    if mode == "already_spanned":
        monkeypatch.setattr(wrapper, "_already_spanned", lambda: True)
    token = None
    if mode == "suppressed":
        token = context.attach(
            context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
        )
    try:
        stream = wrapper.aexecute_stream(lambda: underlying, workflow, (), {})
        assert await anext(stream) == "partial"
        await stream.aclose()
    finally:
        if token is not None:
            context.detach(token)
    assert events == ["started", "closed"]
    assert underlying.ag_frame is None
    if mode == "traced":
        assert (
            exporter.get_finished_spans()[0].status.status_code
            == trace.StatusCode.ERROR
        )
    else:
        assert exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_nested_workflow_setup_failure_finishes_span(workflow_tracing):
    tracer, exporter = workflow_tracing

    def fails():
        raise ValueError("source setup failed")

    stream = _WorkflowExecuteWrapper(tracer).aexecute_stream(
        fails, Workflow(name="Synthetic", steps=[]), (), {}
    )
    with pytest.raises(ValueError, match="source setup failed"):
        await anext(stream)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


@pytest.mark.asyncio
async def test_async_workflow_cleanup_failure_finishes_span(workflow_tracing):
    tracer, exporter = workflow_tracing

    async def source():
        try:
            yield "partial"
        finally:
            raise ValueError("cleanup failed")

    stream = _WorkflowWrapper(tracer).arun(
        source, Workflow(name="Synthetic", steps=[]), (), {}
    )
    await anext(stream)
    with pytest.raises(ValueError, match="cleanup failed"):
        await stream.aclose()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_close_snapshots_terminal_result_and_cleanup_wins(
    workflow_tracing, asynchronous, completed, cleanup_error
):
    tracer, exporter = workflow_tracing
    workflow = Workflow(name="Synthetic", steps=[])
    event = SimpleNamespace(
        event="WorkflowCompleted" if completed else "WorkflowStarted",
        content="completed-output" if completed else None,
        run_id="synthetic-run",
    )
    cleaned = []

    def cleanup():
        cleaned.append(trace.get_current_span().get_span_context().span_id)
        event.content = "mutated-during-close"
        event.event = "WorkflowCancelled"
        if cleanup_error:
            raise ValueError("synthetic cleanup failure")

    def source():
        try:
            yield event
        finally:
            cleanup()

    async def async_source():
        try:
            yield event
        finally:
            cleanup()

    wrapper = _WorkflowWrapper(tracer)
    if asynchronous:
        stream = wrapper.arun(async_source, workflow, (), {})
        await anext(stream)
    else:
        stream = wrapper.run(source, workflow, (), {})
        next(stream)
    before = trace.get_current_span().get_span_context()
    if cleanup_error:
        with pytest.raises(ValueError, match="synthetic cleanup failure"):
            if asynchronous:
                await stream.aclose()
            else:
                stream.close()
    elif asynchronous:
        await stream.aclose()
    else:
        stream.close()
    assert trace.get_current_span().get_span_context() == before
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert cleaned == [span.context.span_id]
    assert span.status.status_code == (
        trace.StatusCode.OK
        if completed and not cleanup_error
        else trace.StatusCode.ERROR
    )
    if completed:
        assert span.attributes["output.value"] == "completed-output"
        assert span.attributes["agno.run.id"] == "synthetic-run"
    if cleanup_error:
        assert any(
            event.attributes["exception.message"] == "synthetic cleanup failure"
            for event in span.events
        )
    if asynchronous:
        await stream.aclose()
    else:
        stream.close()
    assert len(exporter.get_finished_spans()) == 1
