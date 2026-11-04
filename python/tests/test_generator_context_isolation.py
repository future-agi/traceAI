"""Generator-owned contexts survive yields without escaping into consumers."""

import asyncio
import sys
from contextvars import ContextVar, copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry.context import get_value
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_current_span

from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig, suppress_tracing
from fi_instrumentation.instrumentation.context_attributes import using_session


@pytest.fixture(params=["chain", "agent", "tool"])
def tracing(request):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig())
    yield tracer, getattr(tracer, request.param), exporter, provider
    provider.shutdown()


@pytest.fixture
def collector():
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
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", payloads
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("close", [False, True])
def test_suppression_across_yield_never_exports_private_content(
    tracing, collector, asynchronous, close, caplog
):
    _, decorate, exporter, provider = tracing
    endpoint, payloads = collector
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )

    @decorate
    def private_step(value):
        return value

    @decorate
    def stream():
        with suppress_tracing():
            try:
                yield "ready"
            finally:
                private_step("SYNTHETIC_PRIVATE_INPUT")

    @decorate
    async def astream():
        with suppress_tracing():
            try:
                await asyncio.sleep(0)
                yield "ready"
            finally:
                await asyncio.sleep(0.001)
                private_step("SYNTHETIC_PRIVATE_INPUT")

    async def consume():
        iterator = astream()
        assert await asyncio.create_task(iterator.__anext__()) == "ready"
        if close:
            await asyncio.create_task(iterator.aclose())
        else:
            with pytest.raises(StopAsyncIteration):
                await asyncio.create_task(iterator.__anext__())

    if asynchronous:
        asyncio.run(consume())
    else:
        iterator = stream()
        assert next(iterator) == "ready"
        if close:
            copy_context().run(iterator.close)
        else:
            with pytest.raises(StopIteration):
                copy_context().run(next, iterator)
    provider.force_flush()
    spans = [
        span
        for payload in payloads
        for resource in ExportTraceServiceRequest.FromString(payload).resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(spans) == 1
    assert spans[0].name in ("stream", "astream")
    assert b"SYNTHETIC_PRIVATE_INPUT" not in b"".join(payloads)
    assert len(exporter.get_finished_spans()) == 1
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("operation", ["next", "send", "throw", "close"])
def test_sync_preserves_nested_span_and_context_tokens(tracing, operation, caplog):
    tracer, decorate, exporter, _ = tracing
    marker = ContextVar("generator-marker", default="consumer")
    observations = []

    @decorate
    def stream():
        token = marker.set("stream")
        try:
            with (
                using_session("stream-session"),
                tracer.start_as_current_span("child") as child,
            ):
                try:
                    yield "ready"
                except ValueError:
                    pass
                finally:
                    observations.append(
                        (
                            get_current_span(),
                            marker.get(),
                            get_value(SpanAttributes.SESSION_ID),
                        )
                    )
                    with tracer.start_as_current_span("descendant"):
                        pass
                    assert observations[-1][0] is child
        finally:
            marker.reset(token)

    with (
        tracer.start_as_current_span("parent") as parent,
        using_session("consumer-session"),
    ):
        iterator = stream()
        assert next(iterator) == "ready"
        assert marker.get() == "consumer"
        assert get_current_span() is parent
        assert get_value(SpanAttributes.SESSION_ID) == "consumer-session"
        consumer_context = copy_context()
        if operation == "close":
            consumer_context.run(iterator.close)
        else:
            action = {
                "next": iterator.__next__,
                "send": lambda: iterator.send("sent"),
                "throw": lambda: iterator.throw(ValueError("handled")),
            }[operation]
            with pytest.raises(StopIteration):
                consumer_context.run(action)
        assert marker.get() == "consumer"
        assert get_current_span() is parent
        assert get_value(SpanAttributes.SESSION_ID) == "consumer-session"
    assert observations[0][1:] == ("stream", "stream-session")
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["descendant"].parent.span_id == spans["child"].context.span_id
    assert spans["descendant"].attributes[SpanAttributes.SESSION_ID] == "stream-session"
    assert spans["child"].parent.span_id == spans["stream"].context.span_id
    assert "Failed to detach context" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_task", [False, True])
@pytest.mark.parametrize("operation", ["next", "send", "throw", "close", "cancel"])
async def test_async_preserves_nested_context_on_every_await(
    tracing, operation, cross_task, caplog
):
    tracer, decorate, exporter, _ = tracing
    marker = ContextVar("generator-marker", default="consumer")
    entered = asyncio.Event()
    observations = []
    tasks = []

    @decorate
    async def stream():
        token = marker.set("stream")
        try:
            with (
                using_session("stream-session"),
                tracer.start_as_current_span("child") as child,
            ):
                try:
                    await asyncio.sleep(0.001)
                    yield "ready"
                    if operation == "cancel":
                        entered.set()
                        await asyncio.Event().wait()
                except ValueError:
                    pass
                finally:
                    tasks.append(asyncio.current_task())
                    await asyncio.sleep(0)
                    await asyncio.sleep(0.001)
                    observations.append(
                        (
                            get_current_span(),
                            marker.get(),
                            get_value(SpanAttributes.SESSION_ID),
                        )
                    )
                    with tracer.start_as_current_span("descendant"):
                        pass
                    assert observations[-1][0] is child
        finally:
            marker.reset(token)

    async def consume(action):
        with (
            tracer.start_as_current_span("consumer") as consumer,
            using_session("consumer-session"),
        ):
            try:
                return await action
            finally:
                assert get_current_span() is consumer
                assert marker.get() == "consumer"
                assert get_value(SpanAttributes.SESSION_ID) == "consumer-session"

    iterator = stream()
    first = consume(iterator.__anext__())
    assert await (asyncio.create_task(first) if cross_task else first) == "ready"
    assert marker.get() == "consumer"
    action = {
        "next": lambda: iterator.__anext__(),
        "send": lambda: iterator.asend("sent"),
        "throw": lambda: iterator.athrow(ValueError("handled")),
        "close": iterator.aclose,
        "cancel": lambda: iterator.__anext__(),
    }[operation]()
    final = consume(action)
    task = asyncio.create_task(final) if cross_task or operation == "cancel" else None
    if operation == "cancel":
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif operation == "close":
        await (task if task is not None else final)
    else:
        with pytest.raises(StopAsyncIteration):
            await (task if task is not None else final)
    assert tasks == [task if task is not None else asyncio.current_task()]
    assert observations[0][1:] == ("stream", "stream-session")
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["descendant"].parent.span_id == spans["child"].context.span_id
    assert spans["descendant"].attributes[SpanAttributes.SESSION_ID] == "stream-session"
    assert spans["child"].parent.span_id == spans["stream"].context.span_id
    assert "Failed to detach context" not in caplog.text


@pytest.mark.skipif(
    sys.version_info < (3, 13), reason="close() returns a value on Python 3.13+"
)
def test_python313_close_return_value(tracing):
    _, decorate, exporter, _ = tracing

    def stream():
        try:
            yield "ready"
        except GeneratorExit:
            return "cleanup-result"

    original = stream()
    wrapped = decorate(stream)()
    assert next(original) == next(wrapped) == "ready"
    assert wrapped.close() == original.close() == "cleanup-result"
    assert wrapped.close() is None
    assert len(exporter.get_finished_spans()) == 1
