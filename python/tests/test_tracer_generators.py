"""T1-PY-CORE-002: offline decorator regressions using fake stream functions."""

import asyncio
from contextvars import ContextVar

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, get_current_span

from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig


@pytest.fixture(params=["chain", "agent", "tool"])
def tracing(request):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("generator-regression"), TraceConfig())
    yield tracer, exporter, request.param
    provider.shutdown()


@pytest.fixture(params=[False, True], ids=["bare", "named"])
def decorate(tracing, request):
    tracer, _, kind = tracing
    decorator = getattr(tracer, kind)
    return decorator(name="stream") if request.param else decorator


def finished(exporter, name="stream"):
    return [span for span in exporter.get_finished_spans() if span.name == name]


def assert_error(exporter):
    spans = finished(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in spans[0].events)


def test_sync_generator_lifetime_output_parent_and_return(tracing, decorate):
    tracer, exporter, kind = tracing
    application_state = ContextVar("application_state", default="before")
    result = object()

    @decorate
    def stream():
        application_state.set("inside")
        with tracer.start_as_current_span("work", fi_span_kind="chain"):
            yield "first"
            yield "second"
        return result

    iterator = stream()
    assert exporter.get_finished_spans() == ()
    with tracer.start_as_current_span("caller", fi_span_kind="chain") as caller:
        assert next(iterator) == "first"
        assert application_state.get() == "inside"
        assert get_current_span() is caller
        assert not finished(exporter)
        with tracer.start_as_current_span("consumer", fi_span_kind="chain"):
            pass
        assert next(iterator) == "second"
        assert get_current_span() is caller
        assert not finished(exporter)
        with pytest.raises(StopIteration) as stopped:
            next(iterator)
        assert stopped.value.value is result
        assert get_current_span() is caller

    span, = finished(exporter)
    child, = finished(exporter, "work")
    consumer, = finished(exporter, "consumer")
    assert span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == kind.upper()
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "second"
    assert span.parent.span_id == caller.get_span_context().span_id
    assert child.parent.span_id == span.context.span_id
    assert consumer.parent.span_id == caller.get_span_context().span_id
    assert span.start_time <= child.start_time <= child.end_time <= span.end_time


def test_sync_generator_late_exception(tracing, decorate):
    tracer, exporter, _ = tracing
    failure = ValueError("fake stream failure")

    @decorate
    def stream():
        with tracer.start_as_current_span("work", fi_span_kind="chain"):
            yield "partial"
        raise failure

    iterator = stream()
    assert not finished(exporter)
    assert next(iterator) == "partial"
    with pytest.raises(ValueError) as raised:
        next(iterator)
    assert raised.value is failure
    assert_error(exporter)
    assert finished(exporter)[0].attributes[SpanAttributes.OUTPUT_VALUE] == "partial"


def test_sync_generator_send_throw_and_close(tracing, decorate):
    tracer, exporter, _ = tracing
    cleanup_parents = []

    @decorate
    def stream():
        try:
            value = yield "ready"
            try:
                yield value
            except LookupError:
                yield "recovered"
        finally:
            cleanup_parents.append(get_current_span().get_span_context().span_id)

    iterator = stream()
    assert not finished(exporter)
    with pytest.raises(TypeError):
        iterator.send("not started")
    assert not finished(exporter)
    assert next(iterator) == "ready"
    assert iterator.send("sent") == "sent"
    assert iterator.throw(LookupError("fake recoverable failure")) == "recovered"
    assert not finished(exporter)
    iterator.close()
    iterator.close()
    span, = finished(exporter)
    assert cleanup_parents == [span.context.span_id]
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "recovered"
    assert span.status.status_code is not StatusCode.ERROR


def test_unstarted_generators_and_argument_errors(tracing, decorate):
    _, exporter, _ = tracing
    entered = []

    @decorate
    def stream(required):
        entered.append(True)
        yield required

    with pytest.raises(TypeError):
        stream()
    iterator = stream("unused")
    iterator.close()
    assert not entered
    assert not finished(exporter)


def test_async_generator_lifetime_output_and_parent(tracing, decorate):
    tracer, exporter, _ = tracing
    application_state = ContextVar("async_application_state", default="before")

    @decorate
    async def stream():
        application_state.set("inside")
        with tracer.start_as_current_span("work", fi_span_kind="chain"):
            await asyncio.sleep(0)
            yield "first"
            await asyncio.sleep(0)
            yield "second"

    async def consume():
        iterator = stream()
        assert not finished(exporter)
        with tracer.start_as_current_span("caller", fi_span_kind="chain") as caller:
            assert await iterator.__anext__() == "first"
            assert application_state.get() == "inside"
            assert get_current_span() is caller
            assert not finished(exporter)
            assert await iterator.__anext__() == "second"
            assert get_current_span() is caller
            assert not finished(exporter)
            with pytest.raises(StopAsyncIteration):
                await iterator.__anext__()
            assert get_current_span() is caller

    asyncio.run(consume())
    span, = finished(exporter)
    child, = finished(exporter, "work")
    caller, = finished(exporter, "caller")
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "second"
    assert span.parent.span_id == caller.context.span_id
    assert child.parent.span_id == span.context.span_id
    assert span.start_time <= child.start_time <= child.end_time <= span.end_time


def test_async_generator_asend_athrow_and_aclose(tracing, decorate):
    _, exporter, _ = tracing
    cleanup_parents = []

    @decorate
    async def stream():
        try:
            value = yield "ready"
            try:
                yield value
            except LookupError:
                yield "recovered"
        finally:
            await asyncio.sleep(0)
            cleanup_parents.append(get_current_span().get_span_context().span_id)

    async def consume():
        iterator = stream()
        assert not finished(exporter)
        with pytest.raises(TypeError):
            await iterator.asend("not started")
        assert not finished(exporter)
        assert await iterator.__anext__() == "ready"
        assert await iterator.asend("sent") == "sent"
        assert await iterator.athrow(LookupError("fake recoverable failure")) == "recovered"
        assert not finished(exporter)
        await iterator.aclose()
        await iterator.aclose()

    asyncio.run(consume())
    span, = finished(exporter)
    assert cleanup_parents == [span.context.span_id]
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "recovered"
    assert span.status.status_code is not StatusCode.ERROR


def test_async_generator_late_exception(tracing, decorate):
    _, exporter, _ = tracing
    failure = ValueError("fake async stream failure")

    @decorate
    async def stream():
        yield "partial"
        raise failure

    async def consume():
        iterator = stream()
        assert not finished(exporter)
        assert await iterator.__anext__() == "partial"
        with pytest.raises(ValueError) as raised:
            await iterator.__anext__()
        assert raised.value is failure

    asyncio.run(consume())
    assert_error(exporter)
    assert finished(exporter)[0].attributes[SpanAttributes.OUTPUT_VALUE] == "partial"


def test_async_generator_cancellation(tracing, decorate):
    _, exporter, _ = tracing

    @decorate
    async def stream():
        yield "ready"
        raise asyncio.CancelledError()

    async def consume():
        iterator = stream()
        assert not finished(exporter)
        assert await iterator.__anext__() == "ready"
        with pytest.raises(asyncio.CancelledError):
            await iterator.__anext__()

    asyncio.run(consume())
    assert_error(exporter)


def test_explicit_stream_output_is_preserved(tracing, decorate):
    _, exporter, _ = tracing

    @decorate
    def stream():
        get_current_span().set_output("explicit output")
        yield "automatic output"

    iterator = stream()
    assert not finished(exporter)
    assert list(iterator) == ["automatic output"]
    span, = finished(exporter)
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "explicit output"
