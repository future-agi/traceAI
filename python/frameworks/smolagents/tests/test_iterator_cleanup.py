"""Owned iterator references must be released before restoring the consumer."""

import gc
import time
from functools import wraps
from inspect import signature
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from smolagents import OpenAIServerModel, ToolCallingAgent
from smolagents.memory import ActionStep
from traceai_smolagents import SmolagentsInstrumentor
from traceai_smolagents._wrappers import _StepWrapper


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # A failed negative control must not contaminate other tests. Assertions
    # below still inspect the actual context before this fixture restores it.
    token = context.attach(context.get_current())
    try:
        yield exporter, provider.get_tracer(__name__), provider
    finally:
        context.detach(token)
        provider.shutdown()


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("termination", ["close", "gc", "exhaust"])
def test_owned_iterator_finalizes_in_producer_context(tracing, mapped, termination):
    exporter, tracer, _ = tracing
    marker = context.create_key("producer-cleanup-state")
    cleanup = []
    decorator_calls = []

    def operation(memory_step):
        with tracer.start_as_current_span("producer-child") as child:
            token = context.attach(context.set_value(marker, "producer-value"))
            try:
                yield child.get_span_context().span_id
                return "done"
            finally:
                cleanup.append((trace.get_current_span().get_span_context().span_id,
                                context.get_value(marker)))
                with tracer.start_as_current_span("producer-cleanup"):
                    pass
                context.detach(token)

    @wraps(operation)
    def decorated(*args, **kwargs):
        decorator_calls.append(trace.get_current_span().get_span_context().span_id)
        iterator = operation(*args, **kwargs)
        return map(lambda item: item, iterator) if mapped else iterator

    iterator = None
    try:
        with tracer.start_as_current_span("caller") as caller:
            iterator = _StepWrapper(tracer)(
                decorated, SimpleNamespace(step_number=1),
                (SimpleNamespace(observations=None, error=None),), {},
            )
            child_id = next(iterator)
            assert trace.get_current_span() is caller
            assert context.get_value(marker) is None
            with tracer.start_as_current_span("consumer") as consumer:
                if termination == "close":
                    assert iterator.close() is None
                elif termination == "gc":
                    del iterator
                    iterator = None
                    gc.collect()
                else:
                    with pytest.raises(StopIteration) as stopped:
                        next(iterator)
                    assert stopped.value.value == "done"
                # Ended Step spans must not become parents of later consumer work.
                assert trace.get_current_span() is consumer
                assert context.get_value(marker) is None
                with tracer.start_as_current_span("consumer-after-cleanup"):
                    pass
            assert trace.get_current_span() is caller
        assert trace.get_current_span().get_span_context().span_id == 0
        assert cleanup == [(child_id, "producer-value")]
        spans = {span.name: span for span in exporter.get_finished_spans()}
        assert len(exporter.get_finished_spans()) == len(spans) == 6
        step = spans["Step 1"]
        assert decorator_calls == [step.context.span_id]
        assert spans["producer-child"].parent.span_id == step.context.span_id
        assert spans["producer-cleanup"].parent.span_id == child_id
        assert spans["producer-cleanup"].end_time <= spans["producer-child"].end_time <= step.end_time
        assert spans["consumer-after-cleanup"].parent.span_id == spans["consumer"].context.span_id
        assert spans["consumer"].parent.span_id == caller.get_span_context().span_id
        assert step.parent.span_id == caller.get_span_context().span_id
        assert step.status.status_code.name == "OK"
    finally:
        if iterator is not None:
            iterator.close()


@pytest.mark.parametrize("mapped", [False, True])
def test_real_sdk_decorated_step_cleanup_restores_consumer(tracing, monkeypatch, mapped):
    exporter, tracer, provider = tracing
    streaming = hasattr(ToolCallingAgent, "_step_stream")
    method = "_step_stream" if streaming else "step"
    original = getattr(ToolCallingAgent, method)
    cleanup = []
    decorator_calls = []
    requests = []

    def record_cleanup(child):
        cleanup.append((child.get_span_context().span_id,
                        trace.get_current_span().get_span_context().span_id))
        with tracer.start_as_current_span("producer-cleanup"):
            pass

    if streaming:
        @wraps(original)
        def child_scope(*args, **kwargs):
            with tracer.start_as_current_span("producer-child") as child:
                try:
                    yield from original(*args, **kwargs)
                finally:
                    record_cleanup(child)
    else:
        # 1.12 has a synchronous Step API, so exercise its real boundary rather
        # than pretending that it supports early-closing a step iterator.
        @wraps(original)
        def child_scope(*args, **kwargs):
            with tracer.start_as_current_span("producer-child") as child:
                try:
                    return original(*args, **kwargs)
                finally:
                    record_cleanup(child)

    @wraps(child_scope)
    def decorated(*args, **kwargs):
        decorator_calls.append(trace.get_current_span().get_span_context().span_id)
        result = child_scope(*args, **kwargs)
        return map(lambda item: item, result) if mapped and streaming else result

    monkeypatch.setattr(ToolCallingAgent, method, decorated)

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={
            "id": "offline", "created": 0, "model": "offline",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "ok", "tool_calls": [{
                    "id": "call-1", "type": "function", "function": {
                        "name": "final_answer", "arguments": '{"answer":"ok"}',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    instrumentor = SmolagentsInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    iterator = None
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            agent = ToolCallingAgent(
                tools=[], verbosity_level=0,
                model=OpenAIServerModel(
                    model_id="offline", api_key="test-not-a-secret",
                    api_base="http://127.0.0.1:9/v1",
                    client_kwargs={"http_client": client},
                ),
            )
            # Initialize SDK memory without advancing its outer agent stream,
            # whose full lifetime is a distinct, pre-existing coverage gap.
            pending = agent.run("return ok", stream=True)
            pending.close()
            agent.step_number = 1
            if "timing" in signature(ActionStep).parameters:
                from smolagents.monitoring import Timing
                memory = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
            else:
                memory = ActionStep(step_number=1, start_time=time.time())
            exporter.clear()
            with tracer.start_as_current_span("caller") as caller:
                if streaming:
                    iterator = getattr(agent, method)(memory)
                    assert next(iterator) is not None
                    assert trace.get_current_span() is caller
                with tracer.start_as_current_span("consumer") as consumer:
                    if streaming:
                        iterator.close()
                    else:
                        assert getattr(agent, method)(memory) == "ok"
                    assert trace.get_current_span() is consumer
                    with tracer.start_as_current_span("consumer-after-close"):
                        pass
                assert trace.get_current_span() is caller
        assert trace.get_current_span().get_span_context().span_id == 0
        assert requests == ["/v1/chat/completions"]
        assert len(cleanup) == 1 and cleanup[0][0] == cleanup[0][1]
        spans = exporter.get_finished_spans()
        (step,) = [span for span in spans if span.name == "Step 1"]
        (child,) = [span for span in spans if span.name == "producer-child"]
        (cleanup_span,) = [span for span in spans if span.name == "producer-cleanup"]
        (consumer_span,) = [span for span in spans if span.name == "consumer"]
        (consumer_work,) = [span for span in spans if span.name == "consumer-after-close"]
        (model,) = [span for span in spans if span.name.startswith(f"{OpenAIServerModel.__name__}.")]
        assert decorator_calls == [step.context.span_id]
        assert step.status.status_code.name == "OK"
        assert step.parent.span_id == (caller if streaming else consumer).get_span_context().span_id
        assert child.parent.span_id == step.context.span_id
        assert model.parent.span_id == child.context.span_id
        assert cleanup_span.parent.span_id == child.context.span_id
        assert cleanup_span.end_time <= child.end_time <= step.end_time
        assert consumer_work.parent.span_id == consumer_span.context.span_id
    finally:
        if iterator is not None:
            iterator.close()
        instrumentor.uninstrument()
