"""Privacy and lifecycle contracts; real SDK routes live in the package tests."""

import asyncio
import json

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, model_serializer

from traceai_instructor import InstructorInstrumentor
from traceai_instructor._wrappers import _RequestWrapper, _json_value


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig())
    yield _RequestWrapper(tracer), exporter, provider
    provider.shutdown()


def test_public_dependency_floor():
    assert InstructorInstrumentor().instrumentation_dependencies() == ("instructor >= 1.0.0",)


def test_objects_are_never_introspected():
    class Client:
        api_key = "synthetic-private-key"
        def __repr__(self):
            raise AssertionError("must not call repr")
    assert _json_value(Client()) is None
    assert _json_value({"nested": [Client(), 3, False]}) == {"nested": [None, 3, False]}


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}, {"nested": [1, "value"]}])
def test_falsey_and_nested_results_are_not_lost(tracing, value):
    wrapper, exporter, _ = tracing
    assert wrapper(lambda: value, None, (), {}) is value
    span, = exporter.get_finished_spans()
    assert json.loads(span.attributes["output.value"]) == value


def test_tuple_result_is_not_mistaken_for_create_with_completion(tracing):
    wrapper, exporter, _ = tracing
    value = ("Ada", 37)
    assert wrapper(lambda: value, None, (), {}) is value
    span, = exporter.get_finished_spans()
    assert json.loads(span.attributes["output.value"]) == ["Ada", 37]


def test_bad_serializer_does_not_break_extraction(tracing, caplog):
    class Result(BaseModel):
        @model_serializer
        def serialize(self):
            raise RuntimeError("custom serializer failed")
    wrapper, exporter, _ = tracing
    result = Result()
    assert wrapper(lambda: result, None, (), {}) is result
    span, = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.OK
    assert "output.value" not in span.attributes
    assert "telemetry serialization failed" in caplog.text
    assert "custom serializer failed" not in caplog.text


def test_real_suppression_context(tracing):
    wrapper, exporter, _ = tracing
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert wrapper(lambda: 42, None, (), {}) == 42
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("async_", [False, True])
def test_failure_privacy_reaches_otlp(tracing, async_):
    _, exporter, provider = tracing
    wrapper = _RequestWrapper(FITracer(provider.get_tracer(__name__),
                                      config=TraceConfig(hide_inputs=True, hide_outputs=True)))
    def fail():
        raise ValueError("synthetic-secret-in-error")
    async def async_fail():
        fail()
    with pytest.raises(ValueError, match="synthetic-secret-in-error"):
        result = wrapper(async_fail if async_ else fail, None, (), {})
        if async_:
            asyncio.run(result)
    span, = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.events[0].name == "exception"
    assert b"synthetic-secret-in-error" not in encode_spans([span]).SerializeToString()


@pytest.mark.parametrize("ending", ["exhaust", "error", "close", "unstarted_close", "throw"])
def test_lazy_sync_stream_lifecycle(tracing, ending):
    wrapper, exporter, provider = tracing
    consumed, parents = [], []
    def stream():
        parents.append(trace.get_current_span().get_span_context().span_id)
        consumed.append(1)
        yield {"value": 1}
        if ending == "error":
            raise ValueError("stream failed")
        yield {"value": 2}
    with provider.get_tracer(__name__).start_as_current_span("parent") as parent:
        result = wrapper(stream, None, (), {})
        assert not consumed and not exporter.get_finished_spans()
        assert trace.get_current_span() is parent
        if ending != "unstarted_close":
            assert next(result) == {"value": 1}
            assert trace.get_current_span() is parent
            assert not exporter.get_finished_spans()
        if ending in ("close", "unstarted_close"):
            result.close()
        elif ending == "error":
            with pytest.raises(ValueError):
                next(result)
        elif ending == "throw":
            with pytest.raises(RuntimeError):
                result.throw(RuntimeError("stop"))
        else:
            assert result.send(None) == {"value": 2}
            with pytest.raises(StopIteration):
                next(result)
        span, = exporter.get_finished_spans()
        assert span.parent.span_id == parent.get_span_context().span_id
        if consumed:
            assert parents == [span.context.span_id]
        assert span.status.status_code == (
            trace.StatusCode.ERROR if ending in ("error", "throw") else
            trace.StatusCode.UNSET if "close" in ending else trace.StatusCode.OK)
        result.close()
        assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exhaust", "error", "close", "unstarted_close", "cancel", "throw"])
async def test_lazy_async_stream_lifecycle(tracing, ending):
    wrapper, exporter, _ = tracing
    consumed = []
    async def stream():
        consumed.append(1)
        yield {"value": 1}
        if ending == "error":
            raise ValueError("stream failed")
        if ending == "cancel":
            raise asyncio.CancelledError()
        yield {"value": 2}
    result = wrapper(stream, None, (), {})
    assert not consumed and not exporter.get_finished_spans()
    if ending != "unstarted_close":
        assert await result.__anext__() == {"value": 1}
        assert not trace.get_current_span().get_span_context().is_valid
    if "close" in ending:
        await result.aclose()
    elif ending == "throw":
        with pytest.raises(RuntimeError):
            await result.athrow(RuntimeError("stop"))
    elif ending in ("error", "cancel"):
        with pytest.raises(ValueError if ending == "error" else asyncio.CancelledError):
            await result.__anext__()
    else:
        assert await result.asend(None) == {"value": 2}
        with pytest.raises(StopAsyncIteration):
            await result.__anext__()
    span, = exporter.get_finished_spans()
    assert span.status.status_code == (
        trace.StatusCode.ERROR if ending in ("error", "cancel", "throw") else
        trace.StatusCode.UNSET if "close" in ending else trace.StatusCode.OK)
    await result.aclose()
    assert len(exporter.get_finished_spans()) == 1
