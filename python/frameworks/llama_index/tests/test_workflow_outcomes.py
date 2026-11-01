"""Failed, timed-out and cancelled workflows must not export as OK.

Standalone Workflows before 2.16 end the run span from the handler's done
callback with ``result=None`` and never call ``span_drop``. From 2.16 Workflows
reports cancellation with ``SpanCancelledEvent`` and exits the span normally.
The Workflow class embedded in llama-index-core before 0.12.43 exits a
user-cancelled run with ``result=None`` from core 0.12.2. These tests run the
real SDK offline on whichever version is installed.
"""

import asyncio
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_llamaindex import LlamaIndexInstrumentor

try:
    from workflows import Workflow, step
    from workflows.errors import WorkflowCancelledByUser, WorkflowTimeoutError
    from workflows.events import StartEvent, StopEvent
except ImportError:
    # llama-index-core before 0.12.43 embeds Workflow; the 0.10.43 floor has none.
    pytest.importorskip("llama_index.core.workflow")
    from llama_index.core.workflow import StartEvent, StopEvent, Workflow, step
    from llama_index.core.workflow.errors import WorkflowCancelledByUser, WorkflowTimeoutError

CANCELLED = "llama_index.cancelled"
# The embedded class names spans after the defining class, so a subclass's
# run span is "Workflow.run"; standalone Workflows uses the subclass name.
EMBEDDED = Workflow.__module__.startswith("llama_index.core.")


class Boom(Workflow):
    @step
    async def go(self, ev: StartEvent) -> StopEvent:
        raise ValueError("synthetic step failure")


class Slow(Workflow):
    @step
    async def go(self, ev: StartEvent) -> StopEvent:
        await asyncio.sleep(5)
        return StopEvent(result="late")


class Echo(Workflow):
    @step
    async def go(self, ev: StartEvent) -> StopEvent:
        await asyncio.sleep(0.05)
        if ev.fail:
            raise ValueError("synthetic step failure")
        return StopEvent(result=ev.text)


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def run_name(workflow_class):
    return "Workflow.run" if EMBEDDED else f"{workflow_class.__name__}.run"


def spans_by_name(exporter):
    spans = exporter.get_finished_spans()
    by_name = {span.name: span for span in spans}
    # Workflows 1.x adds an internal _done step; nothing else is expected.
    assert len(by_name) == len(spans)
    return by_name


def assert_failed(span):
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]
    assert CANCELLED not in span.attributes


def assert_cancelled(span):
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert span.attributes[CANCELLED] is True
    # Cancellation is not an exception, so no exception event is recorded.
    assert not span.events


def test_failed_step_marks_run_error(exporter):
    async def main():
        await Boom(timeout=5).run()

    # Workflows 1.x wraps the step error in WorkflowRuntimeError.
    with pytest.raises(Exception, match="synthetic step failure"):
        asyncio.run(main())
    by_name = spans_by_name(exporter)
    assert set(by_name) <= {run_name(Boom), "Boom.go", "Boom._done", "Workflow._done"}
    assert_failed(by_name["Boom.go"])
    assert_failed(by_name[run_name(Boom)])
    assert "synthetic step failure" in by_name[run_name(Boom)].status.description


def test_timeout_marks_run_error_and_step_cancelled(exporter):
    async def main():
        await Slow(timeout=0.2).run()

    with pytest.raises(WorkflowTimeoutError):
        asyncio.run(main())
    by_name = spans_by_name(exporter)
    assert_failed(by_name[run_name(Slow)])
    exception, = by_name[run_name(Slow)].events
    assert exception.attributes["exception.type"].endswith("WorkflowTimeoutError")
    assert_cancelled(by_name["Slow.go"])


def test_user_cancel_marks_run_and_step_cancelled(exporter):
    async def main():
        handler = Slow(timeout=10).run()
        await asyncio.sleep(0.1)
        await handler.cancel_run()
        with pytest.raises(WorkflowCancelledByUser):
            await handler

    asyncio.run(main())
    by_name = spans_by_name(exporter)
    assert_cancelled(by_name[run_name(Slow)])
    assert_cancelled(by_name["Slow.go"])


def test_concurrent_runs_keep_their_own_outcome(exporter):
    async def main():
        workflow = Echo(timeout=5)
        return await asyncio.gather(
            workflow.run(text="first", fail=False),
            workflow.run(text="second", fail=True),
            workflow.run(text="third", fail=False),
            return_exceptions=True,
        )

    first, second, third = asyncio.run(main())
    assert (first, third) == ("first", "third")
    assert "synthetic step failure" in str(second)
    spans = exporter.get_finished_spans()
    roots = {span.context.span_id: span for span in spans if span.name == run_name(Echo)}
    steps = [span for span in spans if span.name == "Echo.go"]
    assert len(roots) == len(steps) == 3
    # Each run reports its own step's outcome, not a neighbour's.
    for child in steps:
        root = roots[child.parent.span_id]
        assert root.status.status_code is child.status.status_code
    failed = [span for span in roots.values() if span.status.status_code is StatusCode.ERROR]
    assert len(failed) == 1
    assert_failed(failed[0])


def test_success_path_logs_no_warnings(exporter, caplog):
    async def main():
        return await Echo(timeout=5).run(text="ok", fail=False)

    with caplog.at_level(logging.DEBUG, logger="traceai_llamaindex"):
        assert asyncio.run(main()) == "ok"
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not noisy, [r.getMessage() for r in noisy]
    for span in exporter.get_finished_spans():
        assert span.status.status_code is StatusCode.OK


def test_run_is_patched_only_before_workflows_2_16_and_restored():
    import re
    from importlib.metadata import version

    from traceai_llamaindex._handler import instrument_workflow_runs

    if EMBEDDED:
        legacy = True
    else:
        major, minor = map(
            int, re.match(r"(\d+)\.(\d+)", version("llama-index-workflows")).groups()
        )
        legacy = (major, minor) < (2, 16)
    original = Workflow.__dict__["run"]
    instrumentor = LlamaIndexInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider())
    try:
        patched = Workflow.__dict__["run"]
        if legacy:
            assert patched is not original
            assert patched.__wrapped__ is original
        else:
            # 2.16+ drops a failed run span itself; nothing to patch.
            assert patched is original
        # A second patch pass must not stack another wrapper.
        assert instrument_workflow_runs() == []
        assert Workflow.__dict__["run"] is patched
    finally:
        instrumentor.uninstrument()
    assert Workflow.__dict__["run"] is original
