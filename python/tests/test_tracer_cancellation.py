"""Interrupted manual spans must preserve the unwind and export an error."""

import asyncio

import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.instrumentation._tracers import FITracer
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


@pytest.mark.parametrize(
    "error_type", [KeyboardInterrupt, asyncio.CancelledError, GeneratorExit, ValueError]
)
@pytest.mark.parametrize("record_exception", [False, True])
@pytest.mark.parametrize("set_status", [False, True])
@pytest.mark.parametrize("hide_details", [False, True])
@pytest.mark.parametrize("end_on_exit", [False, True])
def test_context_abort_preserves_flags_and_exception(
    error_type, record_exception, set_status, hide_details, end_on_exit,
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig(hide_error_details=hide_details))
    error = error_type("synthetic-private-abort")
    try:
        with tracer.start_as_current_span("parent") as parent:
            with pytest.raises(error_type) as caught:
                with tracer.start_as_current_span(
                    "child", record_exception=record_exception,
                    set_status_on_exception=set_status, end_on_exit=end_on_exit,
                ) as child:
                    assert trace.get_current_span() is child
                    raise error
            assert caught.value is error
            assert trace.get_current_span() is parent
            if not end_on_exit:
                assert child.is_recording()
                assert not exporter.get_finished_spans()
                child.end()
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        span = next(s for s in spans if s.name == "child")
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code == (StatusCode.ERROR if set_status else StatusCode.UNSET)
        exceptions = [event for event in span.events if event.name == "exception"]
        assert len(exceptions) == int(record_exception)
        wire = encode_spans([span]).SerializeToString()
        if hide_details:
            assert b"synthetic-private-abort" not in wire
        elif record_exception or set_status:
            assert b"synthetic-private-abort" in wire
    finally:
        provider.shutdown()


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, asyncio.CancelledError, GeneratorExit])
def test_registered_processor_exports_interrupted_agent_as_error(batch, error_type):
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="synthetic-cancellation", project_type=ProjectType.OBSERVE,
        span_exporter=exporter, batch=batch, verbose=False,
    )
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig(hide_error_details=True))
    error = error_type("synthetic-private-abort")
    try:
        with pytest.raises(error_type) as caught:
            with tracer.start_as_current_span("order-assistant", fi_span_kind="agent"):
                raise error
        assert caught.value is error
        provider.force_flush()
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert len([event for event in span.events if event.name == "exception"]) == 1
        assert b"synthetic-private-abort" not in encode_spans([span]).SerializeToString()
    finally:
        provider.shutdown()


def test_task_cancellation_remains_cancelled_and_restores_parent():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig())

    async def run():
        started = asyncio.Event()
        pending = asyncio.Event()
        unwound = []

        async def work():
            with tracer.start_as_current_span("cancelled-agent", fi_span_kind="agent"):
                started.set()
                try:
                    await pending.wait()
                finally:
                    unwound.append(True)

        with tracer.start_as_current_span("parent") as parent:
            task = asyncio.create_task(work())
            await started.wait()
            task.cancel("synthetic-cancellation")
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert unwound == [True]
            assert trace.get_current_span() is parent
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        child = next(span for span in spans if span.name == "cancelled-agent")
        assert child.parent.span_id == parent.get_span_context().span_id
        assert child.status.status_code == StatusCode.ERROR
        assert len([event for event in child.events if event.name == "exception"]) == 1

    try:
        asyncio.run(run())
    finally:
        provider.shutdown()


def test_generator_close_unwinds_manual_agent_once():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig())

    def work():
        with tracer.start_as_current_span("generator-agent", fi_span_kind="agent"):
            yield "synthetic-output"

    try:
        parent = trace.get_current_span()
        generator = work()
        assert next(generator) == "synthetic-output"
        generator.close()
        generator.close()
        assert trace.get_current_span() is parent
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert len([event for event in span.events if event.name == "exception"]) == 1
    finally:
        provider.shutdown()
