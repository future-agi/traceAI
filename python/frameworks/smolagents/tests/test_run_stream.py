"""T5-PY-006: the agent run owns execution, not just iterator creation."""

import gc
from types import SimpleNamespace

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_smolagents._wrappers import (
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_USAGE_TOTAL_TOKENS,
    OUTPUT_VALUE,
    _RunWrapper,
)


class FakeAgent:
    task = "offline task"
    max_steps = 2
    tools = {}
    managed_agents = {}

    def __init__(self):
        self.monitor = SimpleNamespace(
            total_input_token_count=0, total_output_token_count=0
        )


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    token = context.attach(context.get_current())
    try:
        yield exporter, provider.get_tracer(__name__)
    finally:
        context.detach(token)
        provider.shutdown()


def run_spans(exporter):
    return [span for span in exporter.get_finished_spans() if span.name == "FakeAgent.run"]


@pytest.mark.parametrize("answer_shape", ["legacy", "final_step", "return"])
def test_run_stream_finishes_with_actual_output_usage_and_parent(tracing, monkeypatch, answer_shape):
    exporter, tracer = tracing
    agent = FakeAgent()
    calls = []

    # Model the 1.20 final-answer event without executing any model or SDK run.
    class FinalAnswerStep:
        def __init__(self, output):
            self.output = output

    import smolagents.memory
    monkeypatch.setattr(smolagents.memory, "FinalAnswerStep", FinalAnswerStep, raising=False)
    answer = FinalAnswerStep("done") if answer_shape == "final_step" else "done"
    progress = object()

    def produce():
        with tracer.start_as_current_span("model"):
            agent.monitor.total_input_token_count = 7
            agent.monitor.total_output_token_count = 3
        yield progress
        if answer_shape == "return":
            return "done"
        yield answer

    def run(task, stream=False):
        calls.append((task, stream))
        return produce()

    with tracer.start_as_current_span("caller") as caller:
        iterator = _RunWrapper(tracer)(run, agent, ("offline task", True), {})
        assert calls == [("offline task", True)]  # SDK setup remains eager.
        assert not run_spans(exporter)
        # Moving consumption must not move the run's original parent.
        with tracer.start_as_current_span("consumer") as consumer:
            assert next(iterator) is progress
            assert trace.get_current_span() is consumer
            assert not run_spans(exporter)
            if answer_shape != "return":
                assert iterator.send(None) is answer
                assert not run_spans(exporter)  # A final yield is not exhaustion.
            with pytest.raises(StopIteration) as stopped:
                next(iterator)
            assert stopped.value.value == ("done" if answer_shape == "return" else None)
            assert trace.get_current_span() is consumer
        iterator.close()
        assert trace.get_current_span() is caller

    (run_span,) = run_spans(exporter)
    assert run_span.status.status_code.name == "OK"
    assert run_span.attributes[OUTPUT_VALUE] == "done"
    assert run_span.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 7
    assert run_span.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 3
    assert run_span.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 10
    assert run_span.parent.span_id == caller.get_span_context().span_id
    (model,) = [span for span in exporter.get_finished_spans() if span.name == "model"]
    assert model.parent.span_id == run_span.context.span_id
    assert model.end_time <= run_span.end_time


@pytest.mark.parametrize("termination", ["close", "gc"])
def test_unconsumed_run_stream_never_reports_success(tracing, termination):
    exporter, tracer = tracing
    calls = []

    def produce():
        pytest.fail("an unconsumed run must not execute")
        yield

    def run(task, stream=False):
        calls.append(task)
        return produce()

    iterator = _RunWrapper(tracer)(run, FakeAgent(), ("offline task",), {"stream": True})
    assert calls == ["offline task"]
    assert not run_spans(exporter)
    if termination == "close":
        iterator.close()
        iterator.close()
    else:
        del iterator
        gc.collect()
    spans = run_spans(exporter)
    assert len(spans) <= 1
    assert all(span.status.status_code.name != "OK" for span in spans)
    assert all(OUTPUT_VALUE not in span.attributes for span in spans)
    assert all(GEN_AI_USAGE_TOTAL_TOKENS not in span.attributes for span in spans)


@pytest.mark.parametrize("termination", ["next", "send", "throw", "error", "close", "close_error"])
def test_run_stream_protocol_keeps_producer_context_and_consumer_isolation(tracing, termination):
    exporter, tracer = tracing
    agent = FakeAgent()
    marker = context.create_key("run-producer-marker")
    failure = ValueError("offline producer failure")
    cleanup_failure = ValueError("offline cleanup failure")
    received = []
    cleanup = []

    def produce():
        with tracer.start_as_current_span("producer-child") as child:
            token = context.attach(context.set_value(marker, "producer-value"))
            try:
                try:
                    value = yield child.get_span_context().span_id
                    received.append(value)
                except ValueError as exc:
                    assert exc is failure
                    received.append("recovered")
                    yield "recovered"
                if termination == "error":
                    raise failure
                agent.monitor.total_input_token_count = 7
                agent.monitor.total_output_token_count = 3
                return "done"
            finally:
                cleanup.append((trace.get_current_span().get_span_context().span_id,
                                context.get_value(marker)))
                with tracer.start_as_current_span("cleanup"):
                    pass
                context.detach(token)
                if termination == "close_error":
                    raise cleanup_failure

    def run(task, stream=False):
        return produce()

    iterator = None
    try:
        with tracer.start_as_current_span("caller") as caller:
            iterator = _RunWrapper(tracer)(run, agent, ("offline task",), {"stream": True})
            child_id = next(iterator)
            assert trace.get_current_span() is caller
            assert context.get_value(marker) is None
            assert not run_spans(exporter)
            with tracer.start_as_current_span("consumer") as consumer:
                if termination == "throw":
                    assert iterator.throw(failure) == "recovered"
                    assert trace.get_current_span() is consumer
                    assert context.get_value(marker) is None
                    assert not run_spans(exporter)
                if termination in {"next", "send", "throw"}:
                    with pytest.raises(StopIteration) as stopped:
                        if termination == "send":
                            iterator.send("payload")
                        else:
                            next(iterator)
                    assert stopped.value.value == "done"
                elif termination in {"error", "close_error"}:
                    with pytest.raises(ValueError) as caught:
                        if termination == "error":
                            next(iterator)
                        else:
                            iterator.close()
                    assert caught.value is (failure if termination == "error" else cleanup_failure)
                else:
                    assert iterator.close() is None
                assert trace.get_current_span() is consumer
                assert context.get_value(marker) is None
            iterator.close()
            assert trace.get_current_span() is caller
        assert cleanup == [(child_id, "producer-value")]
        if termination == "send":
            assert received == ["payload"]
        elif termination == "throw":
            assert received == ["recovered"]
        spans = {span.name: span for span in exporter.get_finished_spans()}
        (run_span,) = run_spans(exporter)
        assert run_span.parent.span_id == caller.get_span_context().span_id
        assert spans["producer-child"].parent.span_id == run_span.context.span_id
        assert spans["cleanup"].parent.span_id == child_id
        assert spans["cleanup"].end_time <= spans["producer-child"].end_time <= run_span.end_time
        assert spans["consumer"].parent.span_id == caller.get_span_context().span_id
        if termination in {"error", "close_error"}:
            assert run_span.status.status_code.name == "ERROR"
            assert len(run_span.events) == 1
        elif termination == "close":
            assert run_span.status.status_code.name != "OK"
        else:
            assert run_span.status.status_code.name == "OK"
            assert run_span.attributes[OUTPUT_VALUE] == "done"
            assert run_span.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 10
        if termination in {"error", "close", "close_error"}:
            assert OUTPUT_VALUE not in run_span.attributes
    finally:
        if iterator is not None:
            iterator.close()


@pytest.mark.parametrize("termination", ["close", "gc"])
def test_run_ordinary_iterator_cleanup_uses_producer_context(tracing, termination):
    exporter, tracer = tracing
    marker = context.create_key("run-cleanup-marker")
    cleanup = []

    def produce():
        with tracer.start_as_current_span("child") as child:
            token = context.attach(context.set_value(marker, "producer-value"))
            try:
                yield child.get_span_context().span_id
            finally:
                cleanup.append((trace.get_current_span().get_span_context().span_id,
                                context.get_value(marker)))
                context.detach(token)

    def run(task, stream=False):
        return map(lambda value: value, produce())

    with tracer.start_as_current_span("caller") as caller:
        iterator = _RunWrapper(tracer)(run, FakeAgent(), ("offline task",), {"stream": True})
        child_id = next(iterator)
        assert trace.get_current_span() is caller
        if termination == "close":
            iterator.close()
        else:
            del iterator
            gc.collect()
        assert trace.get_current_span() is caller
        assert context.get_value(marker) is None
    assert cleanup == [(child_id, "producer-value")]
    (run_span,) = run_spans(exporter)
    assert run_span.status.status_code.name != "OK"
    (child,) = [span for span in exporter.get_finished_spans() if span.name == "child"]
    assert child.parent.span_id == run_span.context.span_id
    assert child.end_time <= run_span.end_time


@pytest.mark.parametrize("stream", [False, True])
def test_run_setup_error_still_raises_eagerly(tracing, stream):
    exporter, tracer = tracing
    failure = ValueError("offline setup failure")

    def run(task, stream=False):
        raise failure

    with pytest.raises(ValueError) as caught:
        _RunWrapper(tracer)(run, FakeAgent(), ("offline task",), {"stream": stream})
    assert caught.value is failure
    (span,) = run_spans(exporter)
    assert span.status.status_code.name == "ERROR"
    assert len(span.events) == 1


def test_nonstreaming_and_suppressed_runs_are_unchanged(tracing):
    exporter, tracer = tracing
    agent = FakeAgent()
    output = object()

    def run(task, stream=False):
        agent.monitor.total_input_token_count = 7
        agent.monitor.total_output_token_count = 3
        return output

    assert _RunWrapper(tracer)(run, agent, ("offline task",), {}) is output
    (span,) = run_spans(exporter)
    assert span.attributes[OUTPUT_VALUE] == str(output)
    assert span.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 10
    assert span.status.status_code.name == "OK"
    exporter.clear()
    token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert _RunWrapper(tracer)(run, agent, ("offline task",), {"stream": True}) is output
        assert not exporter.get_finished_spans()
    finally:
        context.detach(token)
