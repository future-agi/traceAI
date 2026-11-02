"""Step and Parallel stream owners must close their SDK source iterators."""

import pytest
from agno.workflow.parallel import Parallel
from agno.workflow.step import Step
from agno.workflow.types import StepOutput
from openinference.instrumentation import OITracer, TraceConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_agno._wrappers._workflow_wrapper import _ParallelWrapper, _StepWrapper


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield OITracer(provider.get_tracer("source-ownership"), config=TraceConfig()), exporter
    provider.shutdown()


@pytest.mark.parametrize("kind", ["step", "parallel"])
def test_sync_early_close_releases_underlying(tracing, kind):
    tracer, exporter = tracing
    step = Step(name="Child", executor=lambda i: StepOutput(content="ok"))
    instance = step if kind == "step" else Parallel(step, name="Container")
    wrapper = _StepWrapper(tracer) if kind == "step" else _ParallelWrapper(tracer)
    events = []
    cleanup_parent = []

    def source():
        try:
            yield StepOutput(content="first")
            yield StepOutput(content="second")
        finally:
            events.append("closed")
            cleanup_parent.append(trace.get_current_span().get_span_context().span_id)

    raw = source()
    stream = (wrapper.run if kind == "step" else wrapper.execute)(
        lambda: raw, instance, (), {}
    )
    next(stream)
    stream.close()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert events == ["closed"] and raw.gi_frame is None
    assert cleanup_parent == [spans[0].context.span_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["step", "parallel"])
async def test_async_early_close_releases_underlying(tracing, kind):
    tracer, exporter = tracing

    async def execute(i):
        return StepOutput(content="ok")

    step = Step(name="Child", executor=execute)
    instance = step if kind == "step" else Parallel(step, name="Container")
    wrapper = _StepWrapper(tracer) if kind == "step" else _ParallelWrapper(tracer)
    events = []
    cleanup_parent = []

    async def source():
        try:
            yield StepOutput(content="first")
            yield StepOutput(content="second")
        finally:
            events.append("closed")
            cleanup_parent.append(trace.get_current_span().get_span_context().span_id)

    raw = source()
    stream = (wrapper.arun if kind == "step" else wrapper.aexecute)(
        lambda: raw, instance, (), {}
    )
    await anext(stream)
    await stream.aclose()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert events == ["closed"] and raw.ag_frame is None
    assert cleanup_parent == [spans[0].context.span_id]
