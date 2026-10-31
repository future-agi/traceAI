"""Real SDK regressions for lazy custom-application streams (no vendor calls)."""

import asyncio
import gc
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry.context import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    attach,
    detach,
    set_value,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
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
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig())
    yield tracer, getattr(tracer, request.param), exporter, request.param
    provider.shutdown()


def assert_parent_restored(parent):
    assert get_current_span().get_span_context() == parent.get_span_context()


def test_sync_lifetime_parenting_and_return(tracing):
    tracer, decorate, exporter, kind = tracing
    active = []

    @decorate(name="stream")
    def stream(count=2):
        for index in range(count):
            active.append(get_current_span())
            with tracer.start_as_current_span("child"):
                yield index
        return "finished"

    with tracer.start_as_current_span("parent") as parent:
        iterator = stream()
        assert inspect.isgenerator(iterator)
        assert not exporter.get_finished_spans()
        assert next(iterator) == 0
        assert active[0].is_recording()
        assert_parent_restored(parent)
        # The user's own child context is still open inside their generator;
        # resuming under the stream must not leak it into the consumer.
        assert next(iterator) == 1
        assert_parent_restored(parent)
        with pytest.raises(StopIteration) as stopped:
            next(iterator)
        assert stopped.value.value == "finished"
        assert_parent_restored(parent)
    spans = exporter.get_finished_spans()
    stream_span = next(span for span in spans if span.name == "stream")
    assert stream_span.parent.span_id == parent.get_span_context().span_id
    assert stream_span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == kind.upper()
    assert stream_span.attributes[SpanAttributes.OUTPUT_VALUE] == "finished"
    assert all(
        span.parent.span_id == stream_span.context.span_id
        for span in spans
        if span.name == "child"
    )
    assert len([span for span in spans if span.name == "stream"]) == 1


@pytest.mark.asyncio
async def test_async_lifetime_and_cross_task_context(tracing):
    tracer, decorate, exporter, _ = tracing
    active = []

    @decorate
    async def stream():
        for value in range(2):
            active.append(get_current_span())
            with tracer.start_as_current_span("child"):
                pass
            await asyncio.sleep(0)
            yield value

    with tracer.start_as_current_span("parent") as parent:
        iterator = stream()
        assert inspect.isasyncgen(iterator)
        assert not exporter.get_finished_spans()
        assert await iterator.__anext__() == 0
        assert active[0].is_recording()
        assert_parent_restored(parent)
        assert await asyncio.create_task(iterator.__anext__()) == 1
        assert_parent_restored(parent)
        with pytest.raises(StopAsyncIteration):
            await asyncio.create_task(iterator.__anext__())
        assert_parent_restored(parent)
    stream_span = next(
        span for span in exporter.get_finished_spans() if span.name == "stream"
    )
    assert stream_span.parent.span_id == parent.get_span_context().span_id
    assert all(
        span.get_span_context().span_id == stream_span.context.span_id
        for span in active
    )
    assert SpanAttributes.OUTPUT_VALUE not in stream_span.attributes


def test_sync_error_after_yield(tracing):
    _, decorate, exporter, _ = tracing
    failure = ValueError("synthetic failure")

    @decorate
    def stream():
        yield 1
        raise failure

    iterator = stream()
    next(iterator)
    with pytest.raises(ValueError) as caught:
        next(iterator)
    assert caught.value is failure
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1
    assert span.attributes["error.type"].endswith("ValueError")
    iterator.close()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_async_error_after_yield(tracing):
    _, decorate, exporter, _ = tracing
    failure = ValueError("synthetic failure")

    @decorate
    async def stream():
        yield 1
        raise failure

    iterator = stream()
    await iterator.__anext__()
    with pytest.raises(ValueError) as caught:
        await iterator.__anext__()
    assert caught.value is failure
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1
    await iterator.aclose()
    assert len(exporter.get_finished_spans()) == 1


def test_sync_send_throw_and_close(tracing):
    tracer, decorate, exporter, _ = tracing
    cleanup = []

    @decorate
    def stream():
        try:
            value = yield "ready"
            try:
                yield value
            except ValueError:
                yield "recovered"
        finally:
            cleanup.append(get_current_span().is_recording())

    with tracer.start_as_current_span("parent") as parent:
        iterator = stream()
        assert next(iterator) == "ready"
        assert iterator.send("sent") == "sent"
        assert iterator.throw(ValueError("handled")) == "recovered"
        assert not exporter.get_finished_spans()
        iterator.close()
        iterator.close()
        assert_parent_restored(parent)
    assert cleanup == [True]
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.UNSET
    assert not span.events
    assert SpanAttributes.OUTPUT_VALUE not in span.attributes


@pytest.mark.asyncio
async def test_async_send_throw_and_close(tracing):
    tracer, decorate, exporter, _ = tracing
    cleanup = []

    @decorate
    async def stream():
        try:
            value = yield "ready"
            try:
                yield value
            except ValueError:
                yield "recovered"
        finally:
            await asyncio.sleep(0)
            cleanup.append(get_current_span().is_recording())

    with tracer.start_as_current_span("parent") as parent:
        iterator = stream()
        assert await iterator.__anext__() == "ready"
        assert await iterator.asend("sent") == "sent"
        assert await iterator.athrow(ValueError("handled")) == "recovered"
        assert not exporter.get_finished_spans()
        await asyncio.create_task(iterator.aclose())
        await iterator.aclose()
        assert_parent_restored(parent)
    assert cleanup == [True]
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.UNSET
    assert not span.events


@pytest.mark.asyncio
async def test_cancellation_while_awaiting(tracing):
    tracer, decorate, exporter, _ = tracing
    started = asyncio.Event()
    cleanup = []

    @decorate
    async def stream():
        try:
            yield 1
            started.set()
            await asyncio.Event().wait()
        finally:
            cleanup.append(get_current_span().is_recording())

    with tracer.start_as_current_span("parent") as parent:
        iterator = stream()
        assert await iterator.__anext__() == 1
        task = asyncio.create_task(iterator.__anext__())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_parent_restored(parent)
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["error.type"].endswith("CancelledError")
        assert len(span.events) == 1
        await iterator.aclose()
    assert cleanup == [True]


def test_sync_abandonment_and_unstarted_close(tracing):
    _, decorate, exporter, _ = tracing
    cleanup = []

    @decorate
    def stream():
        try:
            yield 1
        finally:
            cleanup.append(get_current_span().is_recording())

    stream().close()
    assert not exporter.get_finished_spans()
    iterator = stream()
    next(iterator)
    del iterator
    gc.collect()
    assert cleanup == [True]
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_async_unstarted_close(tracing):
    _, decorate, exporter, _ = tracing

    @decorate
    async def stream():
        yield 1

    await stream().aclose()
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_empty_stream(tracing, asynchronous):
    _, decorate, exporter, _ = tracing

    @decorate
    def stream():
        yield from ()

    @decorate
    async def astream():
        for value in ():
            yield value

    async def consume():
        return [value async for value in astream()]

    assert (asyncio.run(consume()) if asynchronous else list(stream())) == []
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET
    assert SpanAttributes.OUTPUT_VALUE not in span.attributes


def test_suppression_and_manual_output(tracing):
    _, decorate, exporter, _ = tracing

    @decorate
    def stream():
        get_current_span().set_output("manual")
        yield "chunk"
        return "return value"

    token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert list(stream()) == ["chunk"]
    finally:
        detach(token)
    assert not exporter.get_finished_spans()
    assert list(stream()) == ["chunk"]
    (span,) = exporter.get_finished_spans()
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "manual"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_close_failure_is_recorded(tracing, asynchronous):
    _, decorate, exporter, _ = tracing

    @decorate
    def stream():
        try:
            yield 1
        finally:
            raise ValueError("cleanup failed")

    @decorate
    async def astream():
        try:
            yield 1
        finally:
            raise ValueError("cleanup failed")

    async def consume():
        iterator = astream()
        await iterator.__anext__()
        with pytest.raises(ValueError, match="cleanup failed"):
            await iterator.aclose()

    if asynchronous:
        asyncio.run(consume())
    else:
        iterator = stream()
        next(iterator)
        with pytest.raises(ValueError, match="cleanup failed"):
            iterator.close()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1


def test_async_loop_shutdown_finalizes_partial_stream(tracing):
    _, decorate, exporter, _ = tracing
    cleanup = []

    @decorate
    async def stream():
        try:
            yield 1
        finally:
            # The loop may close the underlying generator before the wrapper.
            # Deterministic cleanup under the span requires explicit aclose().
            cleanup.append(True)

    async def consume():
        iterator = stream()
        await iterator.__anext__()
        return iterator

    iterator = asyncio.run(consume())
    assert inspect.isasyncgen(iterator)
    assert cleanup == [True]
    assert len(exporter.get_finished_spans()) == 1


def test_bound_methods_and_ordinary_calls_keep_contract(tracing):
    _, decorate, exporter, _ = tracing

    class Application:
        @decorate
        def stream(self, count=1):
            yield from range(count)

        @classmethod
        @decorate
        def class_stream(cls):
            yield cls.__name__

        @staticmethod
        @decorate
        async def async_stream():
            yield 1

    @decorate
    def ordinary(value=1):
        return value

    @decorate
    async def coroutine(value=2):
        return value

    async def consume():
        assert [value async for value in Application.async_stream()] == [1]
        assert await coroutine() == 2

    assert str(inspect.signature(Application().stream)) == "(count=1)"
    assert list(Application().stream()) == [0]
    assert list(Application.class_stream()) == ["Application"]
    assert ordinary() == 1
    asyncio.run(consume())
    assert len(exporter.get_finished_spans()) == 5
    assert exporter.get_finished_spans()[0].name == "Application.stream"


def test_long_stream_does_not_retain_chunks(tracing):
    import weakref

    _, decorate, exporter, _ = tracing
    live = weakref.WeakSet()

    class Chunk:
        pass

    @decorate
    def stream():
        for _ in range(10_000):
            value = Chunk()
            live.add(value)
            yield value

    for chunk in stream():
        assert len(live) <= 2
    assert len(live) == 1
    (span,) = exporter.get_finished_spans()
    assert SpanAttributes.OUTPUT_VALUE not in span.attributes


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError])
def test_otlp_wire_privacy(asynchronous, failure):
    payloads = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={},
                timeout=5,
            )
        )
    )
    tracer = FITracer(
        provider.get_tracer(__name__),
        TraceConfig(
            hide_inputs=True,
            hide_outputs=True,
            hide_error_details=True,
        ),
    )

    @tracer.chain
    def stream(secret):
        assert get_current_span().is_recording()
        yield "private chunk"
        raise failure("private failure")

    @tracer.chain
    async def astream(secret):
        assert get_current_span().is_recording()
        yield "private chunk"
        raise failure("private failure")

    async def consume():
        iterator = astream("private input")
        assert await iterator.__anext__() == "private chunk"
        assert not payloads
        with pytest.raises(failure):
            await iterator.__anext__()

    try:
        if asynchronous:
            asyncio.run(consume())
        else:
            iterator = stream("private input")
            assert next(iterator) == "private chunk"
            assert not payloads
            with pytest.raises(failure):
                next(iterator)
        provider.force_flush()
        assert len(payloads) == 1
        assert b"private" not in payloads[0]
        request = ExportTraceServiceRequest.FromString(payloads[0])
        (span,) = request.resource_spans[0].scope_spans[0].spans
        assert span.status.code == 2
        assert not span.status.message
        assert len(span.events) == 1
        assert {attr.key for attr in span.events[0].attributes} == {
            "exception.type",
            "exception.escaped",
        }
        assert span.end_time_unix_nano >= span.start_time_unix_nano
    finally:
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()
