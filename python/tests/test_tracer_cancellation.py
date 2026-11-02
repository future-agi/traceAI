"""Local aborts must not become successful spans at the FI processor."""
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


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, asyncio.CancelledError, GeneratorExit, ValueError])
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
        exceptions = [e for e in span.events if e.name == "exception"]
        assert len(exceptions) == int(record_exception)
        wire = encode_spans([span]).SerializeToString()
        if hide_details:
            assert b"synthetic-private-abort" not in wire
        elif record_exception or set_status:
            assert b"synthetic-private-abort" in wire
    finally:
        provider.shutdown()


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, asyncio.CancelledError])
def test_registered_processor_exports_interrupted_agent_as_error(batch, error_type):
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="synthetic-cancellation", project_type=ProjectType.OBSERVE,
        span_exporter=exporter, batch=batch, verbose=False,
    )
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig(hide_error_details=True))
    try:
        with pytest.raises(error_type):
            with tracer.start_as_current_span("order-assistant", fi_span_kind="agent"):
                raise error_type("synthetic-private-abort")
        provider.force_flush()
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert len([e for e in span.events if e.name == "exception"]) == 1
        assert b"synthetic-private-abort" not in encode_spans([span]).SerializeToString()
    finally:
        provider.shutdown()
