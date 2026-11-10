"""T3-LITELLM-004: offline stream lifecycle regressions with real FI/OTel spans."""

import asyncio
import os
from contextlib import contextmanager
from types import MethodType, SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Prevent LiteLLM's import-time model cost lookup from using the network.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from traceai_litellm import LiteLLMInstrumentor


CHUNK = {"choices": [{"delta": {"content": "partial"}}]}
REQUEST = {"model": "gpt-4o-mini", "messages": [], "stream": True}


class _TrackedContext:
    def __init__(self, context):
        self.context = context
        self.exits = []
        self.span = None

    def __enter__(self):
        self.span = self.context.__enter__()
        return self.span

    def __exit__(self, *exc_info):
        self.exits.append(exc_info)
        return self.context.__exit__(*exc_info)


class _TrackedTracer:
    def __init__(self, tracer):
        self.tracer = tracer
        self.contexts = []

    def start_as_current_span(self, *args, **kwargs):
        context = _TrackedContext(self.tracer.start_as_current_span(*args, **kwargs))
        # Retain construction contexts even when the provider raises: cleanup
        # must be explicit, not dependent on traceback/generator finalization.
        self.contexts.append(context)
        return context


@contextmanager
def _tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent_tracer = provider.get_tracer("stream-lifecycle-test")
    tracked = _TrackedTracer(FITracer(parent_tracer, config=TraceConfig()))
    try:
        with parent_tracer.start_as_current_span("parent") as parent:
            try:
                yield SimpleNamespace(
                    tracer=tracked, parent_tracer=parent_tracer,
                    exporter=exporter, parent=parent,
                )
            finally:
                # Keep the RED tests isolated, including on assertion failure.
                for context in reversed(tracked.contexts):
                    if context.span is not None and context.span.is_recording():
                        context.__exit__(None, None, None)
    finally:
        provider.shutdown()


def _instrumentor(harness, function_name, provider_call):
    # Exercise the real completion methods without global LiteLLM monkeypatches
    # or BaseInstrumentor's process-wide singleton instrumentation state.
    instrumentor = SimpleNamespace(
        _tracer=harness.tracer,
        original_litellm_funcs={function_name: provider_call},
    )
    if hasattr(LiteLLMInstrumentor, "_get_call_arguments"):
        instrumentor._get_call_arguments = MethodType(
            LiteLLMInstrumentor._get_call_arguments, instrumentor
        )
    return instrumentor


def _assert_finished(harness, failure=None):
    context = harness.tracer.contexts[0]
    assert len(context.exits) == 1
    assert not context.span.is_recording()
    assert trace.get_current_span().get_span_context() == harness.parent.get_span_context()
    spans = harness.exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].context == context.span.get_span_context()
    assert spans[0].parent == harness.parent.get_span_context()
    if failure is not None:
        exc_type, exc_value, traceback = context.exits[0]
        assert exc_type is type(failure)
        assert exc_value is failure
        # The original provider frame must still be in the propagated traceback.
        frames = []
        current = failure.__traceback__
        while current is not None:
            frames.append(current)
            current = current.tb_next
        assert traceback in frames
        assert frames[-1].tb_frame.f_code.co_name in {"provider_call", "chunks"}
        if isinstance(failure, Exception):
            assert spans[0].status.status_code is trace.StatusCode.ERROR
            assert len([event for event in spans[0].events if event.name == "exception"]) == 1
    return context, spans[0]


@pytest.mark.parametrize("stage", ["construction", "iteration"])
@pytest.mark.parametrize("failure_type", [ValueError, asyncio.CancelledError])
def test_sync_stream_failure_ends_and_detaches_span(stage, failure_type):
    failure = failure_type("synthetic stream failure")

    def chunks():
        yield CHUNK
        raise failure

    def provider_call(**kwargs):
        if stage == "construction":
            raise failure
        return chunks()

    with _tracing() as harness:
        instrumentor = _instrumentor(harness, "completion", provider_call)
        stream = None
        with pytest.raises(failure_type) as caught:
            stream = LiteLLMInstrumentor._completion_wrapper(instrumentor, **REQUEST)
            assert next(stream) is CHUNK
            next(stream)
        assert caught.value is failure
        context, _ = _assert_finished(harness, failure)
        with harness.parent_tracer.start_as_current_span("following") as following:
            assert following.parent == harness.parent.get_span_context()
            if stream is not None:
                stream.__exit__(None, None, None)
                assert len(context.exits) == 1
                assert trace.get_current_span().get_span_context() == following.get_span_context()


@pytest.mark.parametrize("stage", ["construction", "iteration"])
@pytest.mark.parametrize("failure_type", [ValueError, asyncio.CancelledError])
def test_async_stream_failure_ends_and_detaches_span(stage, failure_type):
    async def scenario():
        failure = failure_type("synthetic stream failure")

        async def chunks():
            yield CHUNK
            raise failure

        async def provider_call(**kwargs):
            if stage == "construction":
                raise failure
            return chunks()

        # Creation, consumption, assertions and RED cleanup share one task.
        with _tracing() as harness:
            instrumentor = _instrumentor(harness, "acompletion", provider_call)
            stream = None
            with pytest.raises(failure_type) as caught:
                stream = await LiteLLMInstrumentor._acompletion_wrapper(instrumentor, **REQUEST)
                assert await stream.__anext__() is CHUNK
                await stream.__anext__()
            assert caught.value is failure
            context, _ = _assert_finished(harness, failure)
            with harness.parent_tracer.start_as_current_span("following") as following:
                assert following.parent == harness.parent.get_span_context()
                if stream is not None:
                    await stream.close()
                    await stream.close()
                    assert len(context.exits) == 1
                    assert trace.get_current_span().get_span_context() == following.get_span_context()

    asyncio.run(scenario())


@pytest.mark.parametrize("exhaust", [False, True], ids=["early-exit", "exhaustion"])
def test_sync_stream_context_exit_is_idempotent(exhaust):
    def provider_call(**kwargs):
        return iter([CHUNK])

    with _tracing() as harness:
        instrumentor = _instrumentor(harness, "completion", provider_call)
        stream = LiteLLMInstrumentor._completion_wrapper(instrumentor, **REQUEST)
        with stream:
            assert next(stream) is CHUNK
            if exhaust:
                with pytest.raises(StopIteration):
                    next(stream)
        context, span = _assert_finished(harness)
        if exhaust:
            assert span.status.status_code is trace.StatusCode.OK
            assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "partial"
        with harness.parent_tracer.start_as_current_span("following") as following:
            stream.__exit__(None, None, None)
            with pytest.raises(StopIteration):
                next(stream)
            assert len(context.exits) == 1
            assert trace.get_current_span().get_span_context() == following.get_span_context()


@pytest.mark.parametrize("exhaust", [False, True], ids=["early-close", "exhaustion"])
def test_async_stream_close_is_idempotent(exhaust):
    async def scenario():
        async def chunks():
            yield CHUNK

        async def provider_call(**kwargs):
            return chunks()

        with _tracing() as harness:
            instrumentor = _instrumentor(harness, "acompletion", provider_call)
            stream = await LiteLLMInstrumentor._acompletion_wrapper(instrumentor, **REQUEST)
            assert await stream.__anext__() is CHUNK
            if exhaust:
                with pytest.raises(StopAsyncIteration):
                    await stream.__anext__()
            await stream.close()
            context, span = _assert_finished(harness)
            if exhaust:
                assert span.status.status_code is trace.StatusCode.OK
                assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "partial"
            with harness.parent_tracer.start_as_current_span("following") as following:
                await stream.close()
                with pytest.raises(StopAsyncIteration):
                    await stream.__anext__()
                assert len(context.exits) == 1
                assert trace.get_current_span().get_span_context() == following.get_span_context()

    asyncio.run(scenario())
