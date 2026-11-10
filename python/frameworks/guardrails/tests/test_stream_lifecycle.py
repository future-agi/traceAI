"""Generator protocol and per-resume context checks for the legacy boundaries."""

import asyncio
from importlib.util import find_spec

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_guardrails import GuardrailsInstrumentor
from traceai_guardrails._wrap_stream import (
    _AsyncStreamParseCallableWrapper,
    _StreamParseCallableWrapper,
)


@pytest.fixture
def boundary():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("legacy"), config=TraceConfig(hide_inputs=True, hide_outputs=True))
    yield provider, exporter, tracer
    provider.shutdown()


def event():
    trace.get_current_span().add_event(
        "validator_result", {"input": "PRIVATE_INPUT", "output": "PRIVATE_OUTPUT"}
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("finish", ["exhaust", "close", "cancel"])
def test_generator_protocol_and_cleanup(boundary, asynchronous, finish):
    provider, exporter, tracer = boundary
    operations = []

    def source():
        try:
            operations.append("start")
            event()
            value = yield "first"
            operations.append(value)
            try:
                yield "second"
            except ValueError as exc:
                operations.append(str(exc))
                yield "recovered"
            return 42
        finally:
            operations.append("cleanup")
            event()

    async def async_source():
        try:
            operations.append("start")
            event()
            value = yield "first"
            operations.append(value)
            try:
                yield "second"
            except ValueError as exc:
                operations.append(str(exc))
                yield "recovered"
        finally:
            operations.append("cleanup")
            event()

    async def run(parent):
        stream = _AsyncStreamParseCallableWrapper(tracer)(async_source, None, (), {})
        assert not operations
        assert stream.__aiter__() is stream
        assert await anext(stream) == "first"
        assert trace.get_current_span() is parent
        assert await stream.asend("sent") == "second"
        assert trace.get_current_span() is parent
        assert await stream.athrow(ValueError("recover")) == "recovered"
        assert trace.get_current_span() is parent
        if finish == "exhaust":
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        elif finish == "close":
            await stream.aclose()
        else:
            with pytest.raises(asyncio.CancelledError):
                await stream.athrow(asyncio.CancelledError("PRIVATE_CANCEL"))
        assert trace.get_current_span() is parent

    before = context.get_current()
    with provider.get_tracer("application").start_as_current_span("application") as parent:
        if asynchronous:
            asyncio.run(run(parent))
        else:
            stream = _StreamParseCallableWrapper(tracer)(source, None, (), {})
            assert not operations
            assert iter(stream) is stream
            assert next(stream) == "first"
            assert trace.get_current_span() is parent
            assert stream.send("sent") == "second"
            assert trace.get_current_span() is parent
            assert stream.throw(ValueError("recover")) == "recovered"
            assert trace.get_current_span() is parent
            if finish == "exhaust":
                with pytest.raises(StopIteration) as stopped:
                    next(stream)
                assert stopped.value.value == 42
            elif finish == "close":
                stream.close()
            else:
                with pytest.raises(asyncio.CancelledError):
                    stream.throw(asyncio.CancelledError("PRIVATE_CANCEL"))
            assert trace.get_current_span() is parent
    assert context.get_current() is before
    assert operations == ["start", "sent", "recover", "cleanup"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 5
    assert not spans[-1].events
    assert len([e for s in spans for e in s.events if e.name == "validator_result"]) == 2
    assert all(span.status.status_code != trace.StatusCode.ERROR for span in spans)
    assert b"PRIVATE_" not in encode_spans(spans).SerializeToString()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_close_before_start_and_suppressed_consumption(boundary, asynchronous):
    provider, exporter, tracer = boundary
    consumed = []

    def source():
        consumed.append(True)
        yield "chunk"

    async def async_source():
        consumed.append(True)
        yield "chunk"

    async def run():
        unopened = _AsyncStreamParseCallableWrapper(tracer)(async_source, None, (), {})
        await unopened.aclose()
        stream = _AsyncStreamParseCallableWrapper(tracer)(async_source, None, (), {})
        token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            assert [part async for part in stream] == ["chunk"]
        finally:
            context.detach(token)

    if asynchronous:
        asyncio.run(run())
    else:
        unopened = _StreamParseCallableWrapper(tracer)(source, None, (), {})
        unopened.close()
        stream = _StreamParseCallableWrapper(tracer)(source, None, (), {})
        token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            assert list(stream) == ["chunk"]
        finally:
            context.detach(token)
    assert consumed == [True]
    assert not exporter.get_finished_spans()


def test_stream_methods_restored_on_uninstrument(boundary):
    if find_spec("guardrails.telemetry.guard_tracing") is not None:
        pytest.skip("Legacy execution hooks")
    from guardrails.run import AsyncStreamRunner, StreamRunner

    provider, _, _ = boundary
    originals = (StreamRunner.step, AsyncStreamRunner.async_step)
    instrumentor = GuardrailsInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=provider)
        assert StreamRunner.step.__wrapped__ is originals[0]
        assert AsyncStreamRunner.async_step.__wrapped__ is originals[1]
    finally:
        instrumentor.uninstrument()
    assert (StreamRunner.step, AsyncStreamRunner.async_step) == originals
