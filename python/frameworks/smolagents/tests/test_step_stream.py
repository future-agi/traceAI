"""Iterator boundaries must never make a suspended Step span current."""

import json
from functools import wraps
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from smolagents import OpenAIServerModel, Tool, ToolCallingAgent
from traceai_smolagents import SmolagentsInstrumentor
from traceai_smolagents._wrappers import _StepWrapper


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield exporter, provider.get_tracer(__name__), provider
    finally:
        provider.shutdown()


def current_id():
    return trace.get_current_span().get_span_context().span_id


@pytest.mark.parametrize("decorator", ["none", "passthrough", "iterator"])
def test_real_sdk_interleaved_streams_leave_consumer_context_unchanged(tracing, monkeypatch, decorator):
    exporter, tracer, provider = tracing
    requests = []
    decorator_calls = []
    if decorator != "none":
        method = "_step_stream" if hasattr(ToolCallingAgent, "_step_stream") else "step"
        original = getattr(ToolCallingAgent, method)

        @wraps(original)
        def passthrough(*args, **kwargs):
            decorator_calls.append(current_id())
            result = original(*args, **kwargs)
            # A valid ordinary decorator may return an iterator without the
            # generator-only send/throw/close methods. Do not bypass it.
            if decorator == "iterator" and hasattr(result, "__next__"):
                return map(lambda item: item, result)
            return result

        monkeypatch.setattr(ToolCallingAgent, method, passthrough)

    class EchoTool(Tool):
        name = "echo"
        description = "Echo an answer."
        inputs = {"answer": {"type": "string", "description": "Answer to echo"}}
        output_type = "string"

        def __init__(self):
            super().__init__()
            self.calls = []

        def forward(self, answer: str) -> str:
            self.calls.append(answer)
            return answer

    def respond(request):
        requests.append(json.loads(request.content))
        # The first advance of each interleaved stream calls echo; their second
        # steps return the final answer. Even the historical SDK invokes echo
        # as a real tool (1.12 handled final_answer directly without Tool.call).
        tool_name = "echo" if len(requests) <= 2 else "final_answer"
        return httpx.Response(
            200,
            json={
                "id": "offline-completion",
                "created": 0,
                "model": "offline-model",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": '{"answer":"ok"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    instrumentor = SmolagentsInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    streams = []
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            agents = [
                ToolCallingAgent(
                    tools=[EchoTool()],
                    verbosity_level=0,
                    model=OpenAIServerModel(
                        model_id="offline-model",
                        api_key="test-not-a-secret",
                        api_base="http://127.0.0.1:9/v1",
                        client_kwargs={"http_client": client},
                    ),
                )
                for _ in range(2)
            ]
            with tracer.start_as_current_span("caller") as caller:
                streams = [agent.run("offline task", stream=True) for agent in agents]
                for stream in streams:
                    next(stream)
                    assert current_id() == caller.get_span_context().span_id
                with tracer.start_as_current_span("unrelated-consumer-work"):
                    pass
                for stream in streams:
                    list(stream)
                    assert current_id() == caller.get_span_context().span_id
            assert current_id() == 0
    finally:
        # Drain rather than close MultiStepAgent._run_stream: upstream 1.20
        # yields from its finally block, which itself rejects GeneratorExit.
        for stream in streams:
            list(stream)
        instrumentor.uninstrument()

    assert len(requests) == 4
    assert [agent.tools["echo"].calls for agent in agents] == [["ok"], ["ok"]]
    spans = exporter.get_finished_spans()
    steps = [span for span in spans if span.name in {"Step 1", "Step 2"}]
    assert len(steps) == 4
    assert all(span.status.status_code.name == "OK" for span in steps)
    step_ids = {span.context.span_id for span in steps}
    if decorator != "none":
        assert len(decorator_calls) == 4
        assert set(decorator_calls) == step_ids
    model_spans = [
        span for span in spans if span.name.startswith(f"{OpenAIServerModel.__name__}.")
    ]
    assert len(model_spans) == 4
    assert {span.parent.span_id for span in model_spans} == step_ids
    tools = [span for span in spans if span.name == "EchoTool"]
    assert len(tools) == 2
    assert {span.parent.span_id for span in tools} == {
        span.context.span_id for span in steps if span.name == "Step 1"
    }
    (consumer,) = [span for span in spans if span.name == "unrelated-consumer-work"]
    assert consumer.parent.span_id == caller.get_span_context().span_id
    assert all(
        span.parent.span_id == caller.get_span_context().span_id for span in steps
    )


@pytest.mark.parametrize("instrumented", [False, True])
def test_real_sdk_decorated_iterator_preserves_final_answer(tracing, monkeypatch, instrumented):
    exporter, _, provider = tracing
    method = "_step_stream" if hasattr(ToolCallingAgent, "_step_stream") else "step"
    original = getattr(ToolCallingAgent, method)
    decorator_calls = []
    requests = []

    @wraps(original)
    def mapped(*args, **kwargs):
        decorator_calls.append(current_id())
        result = original(*args, **kwargs)
        return map(lambda item: item, result) if hasattr(result, "__next__") else result

    monkeypatch.setattr(ToolCallingAgent, method, mapped)

    def respond(request):
        requests.append(json.loads(request.content))
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
    if instrumented:
        instrumentor.instrument(tracer_provider=provider)
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            agent = ToolCallingAgent(
                tools=[], verbosity_level=0, max_steps=1,
                model=OpenAIServerModel(
                    model_id="offline", api_key="test-not-a-secret",
                    api_base="http://127.0.0.1:9/v1",
                    client_kwargs={"http_client": client},
                ),
            )
            assert agent.run("return ok") == "ok"
        assert current_id() == 0
    finally:
        if instrumented:
            instrumentor.uninstrument()

    assert len(requests) == 1
    assert len(decorator_calls) == 1
    spans = exporter.get_finished_spans()
    if instrumented:
        (step,) = [span for span in spans if span.name == "Step 1"]
        assert step.status.status_code.name == "OK"
        assert decorator_calls == [step.context.span_id]
        (model,) = [
            span for span in spans if span.name.startswith(f"{OpenAIServerModel.__name__}.")
        ]
        assert model.parent.span_id == step.context.span_id
    else:
        assert decorator_calls == [0]
        assert not spans


@pytest.mark.parametrize("termination", ["next", "send_none", "send", "throw", "throw_stop", "close"])
def test_decorated_ordinary_iterator_uses_only_its_actual_protocol(tracing, termination):
    exporter, tracer, _ = tracing
    calls = []
    decorator_calls = []
    failure = StopIteration("injected") if termination == "throw_stop" else ValueError("injected")

    class OrdinaryIterator:
        def __init__(self):
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self):
            calls.append(current_id())
            with tracer.start_as_current_span("ordinary-iterator-work"):
                pass
            self.index += 1
            if self.index > 2:
                raise StopIteration("finished")
            return self.index

    def operation(memory_step):
        pytest.fail("the application decorator must not be bypassed")
        yield

    @wraps(operation)
    def decorated(*args, **kwargs):
        decorator_calls.append(current_id())
        return OrdinaryIterator()

    with tracer.start_as_current_span("caller") as caller:
        iterator = _StepWrapper(tracer)(
            decorated, SimpleNamespace(step_number=1),
            (SimpleNamespace(observations=None, error=None),), {},
        )
        try:
            assert next(iterator) == 1
            assert current_id() == caller.get_span_context().span_id
            with tracer.start_as_current_span("unrelated-consumer") as consumer:
                if termination in {"next", "send_none"}:
                    assert (next(iterator) if termination == "next" else iterator.send(None)) == 2
                    with pytest.raises(StopIteration) as stopped:
                        iterator.send(None)
                    assert stopped.value.value == "finished"
                elif termination == "send":
                    with pytest.raises(AttributeError, match="send"):
                        iterator.send("payload")
                elif termination == "throw":
                    with pytest.raises(ValueError) as caught:
                        iterator.throw(failure)
                    assert caught.value is failure
                elif termination == "throw_stop":
                    with pytest.raises(RuntimeError, match="generator raised StopIteration") as caught:
                        iterator.throw(failure)
                    assert caught.value.__cause__ is failure
                else:
                    assert iterator.close() is None
                assert current_id() == consumer.get_span_context().span_id
            assert current_id() == caller.get_span_context().span_id
        finally:
            iterator.close()

    spans = exporter.get_finished_spans()
    (step,) = [span for span in spans if span.name == "Step 1"]
    assert decorator_calls == [step.context.span_id]
    assert calls == [step.context.span_id] * (3 if termination in {"next", "send_none"} else 1)
    assert step.parent.span_id == caller.get_span_context().span_id
    assert step.status.status_code.name == (
        "ERROR" if termination in {"send", "throw", "throw_stop"} else "OK"
    )
    work = [span for span in spans if span.name == "ordinary-iterator-work"]
    assert len(work) == len(calls)
    assert all(span.parent.span_id == step.context.span_id for span in work)
    (consumer,) = [span for span in spans if span.name == "unrelated-consumer"]
    assert consumer.parent.span_id == caller.get_span_context().span_id
    assert current_id() == 0


@pytest.mark.parametrize("termination", ["complete", "throw", "close", "close_error"])
def test_step_iterator_protocol_and_lifecycle(tracing, termination):
    exporter, tracer, _ = tracing
    active = []
    received = []
    failure = ValueError("iterator failure")
    cleanup_failure = ValueError("cleanup failure")
    memory = SimpleNamespace(observations=None, error=None)

    def operation(memory_step):
        active.append(current_id())
        try:
            received.append((yield "first"))
            active.append(current_id())
            try:
                yield "second"
            except ValueError as exc:
                assert exc is failure
                active.append(current_id())
                raise
            return "finished"
        finally:
            active.append(current_id())
            with tracer.start_as_current_span("iterator-cleanup"):
                pass
            if termination == "close_error":
                raise cleanup_failure

    with tracer.start_as_current_span("caller") as caller:
        iterator = _StepWrapper(tracer)(
            operation, SimpleNamespace(step_number=1), (memory,), {}
        )
        assert next(iterator) == "first"
        assert current_id() == caller.get_span_context().span_id
        assert iterator.send("sent payload") == "second"
        assert received == ["sent payload"]
        assert current_id() == caller.get_span_context().span_id
        if termination == "complete":
            with pytest.raises(StopIteration) as stopped:
                next(iterator)
            assert stopped.value.value == "finished"
        elif termination == "throw":
            with pytest.raises(ValueError) as caught:
                iterator.throw(failure)
            assert caught.value is failure
        elif termination == "close_error":
            with pytest.raises(ValueError) as caught:
                iterator.close()
            assert caught.value is cleanup_failure
        else:
            iterator.close()
        assert current_id() == caller.get_span_context().span_id
        iterator.close()  # Ending twice must not export twice.
        with tracer.start_as_current_span("consumer-after"):
            pass

    spans = exporter.get_finished_spans()
    (step,) = [span for span in spans if span.name == "Step 1"]
    assert active == [step.context.span_id] * len(active)
    assert len(active) == 3 + (termination == "throw")
    assert step.parent.span_id == caller.get_span_context().span_id
    assert step.status.status_code.name == (
        "ERROR" if termination in {"throw", "close_error"} else "OK"
    )
    if termination in {"throw", "close_error"}:
        assert len(step.events) == 1
        assert step.events[0].name == "exception"
    (cleanup,) = [span for span in spans if span.name == "iterator-cleanup"]
    assert cleanup.parent.span_id == step.context.span_id
    (consumer,) = [span for span in spans if span.name == "consumer-after"]
    assert consumer.parent.span_id == caller.get_span_context().span_id
    assert current_id() == 0


def test_step_iterator_can_recover_from_throw(tracing):
    exporter, tracer, _ = tracing
    memory = SimpleNamespace(observations=None, error=None)
    active = []
    error = ValueError("recoverable")

    def operation(memory_step):
        try:
            yield "first"
        except ValueError as exc:
            assert exc is error
            active.append(current_id())
            yield "recovered"
        return 42

    iterator = _StepWrapper(tracer)(
        operation, SimpleNamespace(step_number=1), (memory,), {}
    )
    assert next(iterator) == "first"
    assert current_id() == 0
    assert iterator.throw(error) == "recovered"
    assert current_id() == 0
    with pytest.raises(StopIteration) as stopped:
        next(iterator)
    assert stopped.value.value == 42
    (step,) = exporter.get_finished_spans()
    assert active == [step.context.span_id]
    assert step.status.status_code.name == "OK"
    assert not step.events


def test_unstarted_step_close_does_not_start_span(tracing):
    exporter, tracer, _ = tracing

    def operation(memory_step):
        pytest.fail("closed iterator must not execute")
        yield

    iterator = _StepWrapper(tracer)(
        operation,
        SimpleNamespace(step_number=1),
        (SimpleNamespace(observations=None, error=None),),
        {},
    )
    iterator.close()
    assert not exporter.get_finished_spans()
    assert current_id() == 0
