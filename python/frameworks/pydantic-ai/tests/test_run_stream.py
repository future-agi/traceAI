"""T5-PY-001: preserve the native streaming context-manager protocol."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_pydantic_ai import PydanticAIInstrumentor
from traceai_pydantic_ai._attributes import PydanticAIAttributes


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = PydanticAIInstrumentor()
    instrumentor._tracer = provider.get_tracer(__name__)
    try:
        yield instrumentor, exporter, provider
    finally:
        if instrumentor.is_instrumented:
            instrumentor.uninstrument()
        provider.shutdown()


def wrapped_agent(instrumentor, original):
    class FakeAgent:
        model = None

    FakeAgent.run_stream = instrumentor._wrap_run_stream(original)
    return FakeAgent()


def test_run_stream_supports_async_with_and_keeps_span_current(tracing):
    instrumentor, exporter, _ = tracing
    events = []
    active_spans = []
    dependency = object()

    def observe(event):
        span = trace_api.get_current_span()
        assert span.is_recording()
        assert not exporter.get_finished_spans()
        events.append(event)
        active_spans.append(span)

    async def stream_text():
        observe("consume")
        yield "hello"

    @asynccontextmanager
    async def run_stream(self, prompt, *, deps):
        assert self is agent
        assert prompt == "hello"
        assert deps is dependency
        observe("enter")
        try:
            yield SimpleNamespace(stream_text=stream_text)
        finally:
            observe("exit")

    agent = wrapped_agent(instrumentor, run_stream)

    async def execute():
        previous_span = trace_api.get_current_span()
        async with agent.run_stream("hello", deps=dependency) as result:
            assert [part async for part in result.stream_text()] == ["hello"]
        assert trace_api.get_current_span() is previous_span

    asyncio.run(execute())
    assert events == ["enter", "consume", "exit"]
    assert all(span is active_spans[0] for span in active_spans)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "pydantic_ai.agent.run_stream"
    assert spans[0].status.status_code is trace_api.StatusCode.OK
    assert spans[0].attributes[PydanticAIAttributes.IS_ERROR] is False
    assert spans[0].attributes[PydanticAIAttributes.DURATION_MS] >= 0


@pytest.mark.parametrize("suppress", [False, True], ids=["propagate", "suppress"])
def test_run_stream_preserves_manager_exit_semantics(tracing, suppress):
    instrumentor, exporter, _ = tracing
    error = ValueError("consumer failed")
    exits = []
    entered_result = SimpleNamespace(value="hello")

    class Manager:
        async def __aenter__(self):
            return entered_result

        async def __aexit__(self, exc_type, exc_value, traceback):
            assert trace_api.get_current_span().is_recording()
            exits.append((exc_type, exc_value, traceback))
            return suppress

    agent = wrapped_agent(instrumentor, lambda self, prompt: Manager())

    async def execute():
        async with agent.run_stream("hello") as result:
            assert result.value == "hello"
            raise error

    if suppress:
        asyncio.run(execute())
    else:
        with pytest.raises(ValueError) as caught:
            asyncio.run(execute())
        assert caught.value is error

    assert len(exits) == 1
    assert exits[0][0] is ValueError
    assert exits[0][1] is error
    assert exits[0][2] is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes[PydanticAIAttributes.IS_ERROR] is (not suppress)


@pytest.mark.parametrize("phase", ["factory", "enter", "consume", "exit"])
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
def test_run_stream_ends_span_on_failure(tracing, phase, error_type):
    instrumentor, exporter, _ = tracing
    error = error_type("stream failed")
    exits = []

    async def stream_text():
        if phase == "consume":
            raise error
        yield "hello"

    class Manager:
        async def __aenter__(self):
            if phase == "enter":
                raise error
            return SimpleNamespace(stream_text=stream_text)

        async def __aexit__(self, exc_type, exc_value, traceback):
            assert trace_api.get_current_span().is_recording()
            exits.append(exc_value)
            if phase == "exit":
                raise error
            return False

    def run_stream(self, prompt):
        if phase == "factory":
            raise error
        return Manager()

    agent = wrapped_agent(instrumentor, run_stream)

    async def execute():
        previous_span = trace_api.get_current_span()
        with pytest.raises(error_type) as caught:
            async with agent.run_stream("hello") as result:
                assert [part async for part in result.stream_text()] == ["hello"]
        assert caught.value is error
        assert trace_api.get_current_span() is previous_span

    asyncio.run(execute())
    assert exits == ([] if phase in ("factory", "enter") else
                     [error if phase == "consume" else None])
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is trace_api.StatusCode.ERROR
    assert spans[0].attributes[PydanticAIAttributes.IS_ERROR] is True
    assert spans[0].attributes[PydanticAIAttributes.ERROR_MESSAGE] == "stream failed"
    assert spans[0].attributes[PydanticAIAttributes.DURATION_MS] >= 0
    assert len([event for event in spans[0].events if event.name == "exception"]) == 1


def test_run_stream_test_model_native_async_with(tracing, monkeypatch):
    pydantic_ai = pytest.importorskip("pydantic_ai")
    from pydantic_ai import models
    from pydantic_ai.models.test import TestModel

    # TestModel is local; prohibit real model requests as an additional guard.
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    instrumentor, exporter, provider = tracing
    instrumentor.instrument(tracer_provider=provider)
    agent = pydantic_ai.Agent(TestModel(custom_output_text="hello"))

    async def execute():
        async with agent.run_stream("hello") as result:
            parts = [part async for part in result.stream_text()]
        return parts

    assert asyncio.run(execute())[-1] == "hello"
    spans = [span for span in exporter.get_finished_spans()
             if span.name == "pydantic_ai.agent.run_stream"]
    assert len(spans) == 1
    assert spans[0].status.status_code is trace_api.StatusCode.OK


def test_synchronous_stream_factory_can_return_awaitable_manager(tracing):
    instrumentor, exporter, _ = tracing
    events = []
    result = SimpleNamespace(content="hello")

    @asynccontextmanager
    async def manager():
        assert trace_api.get_current_span().is_recording()
        events.append("enter")
        try:
            yield result
        finally:
            assert trace_api.get_current_span().is_recording()
            events.append("exit")

    async def create_manager():
        events.append("await")
        return manager()

    def run_stream(self, prompt):
        assert prompt == "hello"
        events.append("factory")
        return create_manager()

    agent = wrapped_agent(instrumentor, run_stream)

    async def execute():
        previous = trace_api.get_current_span()
        async with agent.run_stream("hello") as stream:
            assert stream.content == result.content
            assert trace_api.get_current_span().is_recording()
        assert trace_api.get_current_span() is previous

    asyncio.run(execute())
    assert events == ["factory", "await", "enter", "exit"]
    span, = exporter.get_finished_spans()
    assert span.status.status_code is trace_api.StatusCode.OK
