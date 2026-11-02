"""Probe the same StepOutput status contract on Parallel containers."""

import pytest
from agno.workflow.parallel import Parallel
from agno.workflow.step import Step
from agno.workflow.types import StepInput, StepOutput
from opentelemetry import trace
from traceai_agno import configure_agno_tracing

from .test_recall import harness as harness


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_parallel_failed_result_is_not_ok(harness, asynchronous, streaming):
    provider, exporter, tracker, model, settings = harness

    def fail(step_input):
        raise ValueError("synthetic parallel failure")

    async def afail(step_input):
        raise ValueError("synthetic parallel failure")

    def make_parallel():
        return Parallel(
            Step(
                name="Child",
                executor=afail if asynchronous else fail,
                skip_on_failure=True,
                max_retries=0,
            ),
            name="Failure",
        )

    async def run():
        parallel = make_parallel()
        step_input = StepInput(input="test")
        if asynchronous:
            return (
                [x async for x in parallel.aexecute_stream(step_input)]
                if streaming
                else await parallel.aexecute(step_input)
            )
        return (
            list(parallel.execute_stream(step_input))
            if streaming
            else parallel.execute(step_input)
        )

    baseline = await run()
    before = (
        next(x for x in reversed(baseline) if isinstance(x, StepOutput))
        if streaming
        else baseline
    )
    assert before.success is False
    configure_agno_tracing(tracer_provider=provider)
    result = await run()
    after = (
        next(x for x in reversed(result) if isinstance(x, StepOutput))
        if streaming
        else result
    )
    assert after.success is False and after.error == before.error
    spans = exporter.get_finished_spans()
    assert len(spans) == len(tracker.started) == 2
    assert all(s.status.status_code == trace.StatusCode.ERROR for s in spans), [
        (s.name, s.status.status_code) for s in spans
    ]
