"""A suspended producer owns its current context, not its consumer's context."""

from types import SimpleNamespace

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_smolagents._wrappers import _StepWrapper


@pytest.mark.parametrize("termination", ["next", "send", "throw", "close"])
def test_nested_producer_context_survives_each_iterator_boundary(termination):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    seen = []
    error = ValueError("injected")
    marker = context.create_key("producer-state")

    def operation(memory_step):
        with tracer.start_as_current_span("producer-child") as child:
            token = context.attach(context.set_value(marker, "producer-value"))
            try:
                try:
                    received = yield child.get_span_context().span_id
                    if termination == "send":
                        assert received == "payload"
                except ValueError as caught:
                    assert caught is error
                finally:
                    seen.append(trace.get_current_span().get_span_context().span_id)
                    assert context.get_value(marker) == "producer-value"
                    with tracer.start_as_current_span("resumed-or-cleanup"):
                        pass
            finally:
                context.detach(token)
        return "done"

    iterator = None
    try:
        with tracer.start_as_current_span("caller") as caller:
            iterator = _StepWrapper(tracer)(operation, SimpleNamespace(step_number=1),
                (SimpleNamespace(observations=None, error=None),), {})
            child_id = next(iterator)
            assert trace.get_current_span() is caller
            assert context.get_value(marker) is None
            with tracer.start_as_current_span("unrelated-consumer") as consumer:
                if termination == "close":
                    iterator.close()
                else:
                    with pytest.raises(StopIteration) as stopped:
                        if termination == "send":
                            iterator.send("payload")
                        elif termination == "throw":
                            iterator.throw(error)
                        else:
                            next(iterator)
                    assert stopped.value.value == "done"
                assert trace.get_current_span() is consumer
                assert context.get_value(marker) is None
            assert trace.get_current_span() is caller
        assert trace.get_current_span().get_span_context().span_id == 0
        assert seen == [child_id]
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert len(spans) == 5
        assert spans["resumed-or-cleanup"].parent.span_id == child_id
        assert spans["producer-child"].parent.span_id == spans["Step 1"].context.span_id
        assert spans["Step 1"].parent.span_id == caller.get_span_context().span_id
        assert spans["unrelated-consumer"].parent.span_id == caller.get_span_context().span_id
    finally:
        if iterator is not None:
            iterator.close()
        provider.shutdown()
