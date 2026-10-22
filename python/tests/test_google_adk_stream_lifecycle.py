"""Exercise ADK stream wrappers with real OTel spans and async generators."""

import asyncio
from types import SimpleNamespace

import pytest
from google.adk.events import Event
from google.genai import types
from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_google_adk._wrappers import _BaseAgentRunAsync, _RunnerRunAsync

from fi_instrumentation import FITracer, TraceConfig, get_attributes_from_context
from fi_instrumentation.fi_types import SpanAttributes


async def next_event(stream):
    return await stream.__anext__()


def event(text="answer"):
    return Event(
        author="agent",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


@pytest.fixture(params=["runner", "agent"])
def streams(request):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig())
    kind = request.param

    def wrap(source, suffix="one"):
        if kind == "runner":
            wrapper = _RunnerRunAsync(tracer)
            instance = SimpleNamespace(app_name="app")
            kwargs = {"user_id": "user-" + suffix, "session_id": "session-" + suffix}
        else:
            wrapper = _BaseAgentRunAsync(tracer)
            instance = SimpleNamespace(name="agent")
            kwargs = {}
        return wrapper(source, instance, (), kwargs)

    yield wrap, exporter, tracer, kind
    provider.shutdown()


@pytest.mark.asyncio
async def test_direct_iteration_preserves_events_and_output(streams):
    wrap, exporter, _, _ = streams
    expected = event()

    async def source(**kwargs):
        yield expected

    stream = wrap(source)
    assert stream.__aiter__() is stream
    assert await next_event(stream) is expected
    assert not exporter.get_finished_spans()
    with pytest.raises(StopAsyncIteration):
        await next_event(stream)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == expected.model_dump_json(
        exclude_none=True
    )
    await stream.aclose()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_context_is_restored_between_events_and_during_cleanup(streams):
    wrap, exporter, tracer, kind = streams
    observed = []

    async def source(**kwargs):
        try:
            for _ in range(2):
                observed.append(
                    (
                        trace.get_current_span().get_span_context(),
                        dict(get_attributes_from_context()),
                    )
                )
                yield event()
        finally:
            observed.append(
                (
                    trace.get_current_span().get_span_context(),
                    dict(get_attributes_from_context()),
                )
            )

    with tracer.start_as_current_span("consumer") as parent:
        before = context_api.get_current()
        stream = wrap(source)
        async for _ in stream:
            assert context_api.get_current() is before
            assert trace.get_current_span() is parent
        assert context_api.get_current() is before
        (span,) = exporter.get_finished_spans()
        assert span.parent == parent.get_span_context()
        assert all(ctx.span_id == span.context.span_id for ctx, _ in observed)
        assert len(observed) == 3
        if kind == "runner":
            assert all(
                attrs[SpanAttributes.SESSION_ID] == "session-one"
                for _, attrs in observed
            )
            assert all(
                attrs[SpanAttributes.USER_ID] == "user-one" for _, attrs in observed
            )


@pytest.mark.asyncio
async def test_aclose_finishes_span_and_source_once(streams):
    wrap, exporter, tracer, _ = streams
    cleaned = []

    async def source(**kwargs):
        try:
            yield event()
            pytest.fail("aclose must not resume normal execution")
        finally:
            cleaned.append(trace.get_current_span().get_span_context())

    with tracer.start_as_current_span("consumer") as parent:
        stream = wrap(source)
        await next_event(stream.__aiter__())
        await stream.aclose()
        await stream.aclose()
        assert trace.get_current_span() is parent
        (span,) = exporter.get_finished_spans()
        assert cleaned == [span.context]
        assert span.status.status_code == StatusCode.UNSET


@pytest.mark.asyncio
async def test_unstarted_close_is_lazy(streams):
    wrap, exporter, _, _ = streams

    async def source(**kwargs):
        pytest.fail("closing an unstarted stream must not execute it")
        yield event()

    stream = wrap(source)
    await stream.aclose()
    assert not exporter.get_finished_spans()


@pytest.mark.asyncio
async def test_asend_is_forwarded(streams):
    wrap, exporter, _, _ = streams
    sent = []
    first, second = event("first"), event("second")

    async def source(**kwargs):
        sent.append((yield first))
        yield second

    stream = wrap(source)
    assert await stream.asend(None) is first
    assert await stream.asend("feedback") is second
    assert sent == ["feedback"]
    with pytest.raises(StopAsyncIteration):
        await next_event(stream)
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_athrow_can_be_handled_by_source(streams):
    wrap, exporter, _, _ = streams
    recovered = event("recovered")
    problem = ValueError("recoverable")
    caught = []

    async def source(**kwargs):
        try:
            yield event()
        except ValueError as exc:
            caught.append(exc)
            yield recovered

    stream = wrap(source)
    await next_event(stream)
    assert await stream.athrow(problem) is recovered
    with pytest.raises(StopAsyncIteration):
        await next_event(stream)
    (span,) = exporter.get_finished_spans()
    assert caught == [problem]
    assert span.status.status_code == StatusCode.OK
    assert not span.events


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_during_close", [False, True])
async def test_errors_are_preserved_and_recorded_once(streams, fail_during_close):
    wrap, exporter, tracer, _ = streams
    problem = ValueError("source failure")

    async def source(**kwargs):
        try:
            yield event()
            if not fail_during_close:
                raise problem
        finally:
            if fail_during_close:
                raise problem

    with tracer.start_as_current_span("consumer") as parent:
        stream = wrap(source)
        await next_event(stream)
        with pytest.raises(ValueError) as caught:
            if fail_during_close:
                await stream.aclose()
            else:
                await next_event(stream)
        assert caught.value is problem
        assert trace.get_current_span() is parent
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) == 1
        assert span.events[0].name == "exception"


@pytest.mark.asyncio
async def test_cancellation_closes_source_and_finishes_span(streams):
    wrap, exporter, _, _ = streams
    waiting, blocker = asyncio.Event(), asyncio.Event()
    cleaned = []

    async def source(**kwargs):
        try:
            yield event()
            waiting.set()
            await blocker.wait()
        finally:
            cleaned.append(True)

    before = context_api.get_current()
    stream = wrap(source)
    await next_event(stream)
    task = asyncio.create_task(next_event(stream))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]
    assert context_api.get_current() is before
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code != StatusCode.OK
    await stream.aclose()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_nested_span_context_survives_yields(streams):
    wrap, exporter, tracer, _ = streams
    observed = []

    async def source(**kwargs):
        with tracer.start_as_current_span("nested") as nested:
            for _ in range(2):
                observed.append(trace.get_current_span() is nested)
                yield event()

    with tracer.start_as_current_span("consumer") as parent:
        async for _ in wrap(source):
            assert trace.get_current_span() is parent
        assert observed == [True, True]
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert len(spans) == 2
        outer = next(s for s in spans.values() if s.name != "nested")
        assert spans["nested"].parent == outer.context
        assert outer.parent == parent.get_span_context()


@pytest.mark.asyncio
async def test_interleaved_streams_do_not_become_each_others_parent(streams):
    wrap, exporter, tracer, kind = streams
    observed = []

    async def source(**kwargs):
        for _ in range(2):
            observed.append((kwargs, dict(get_attributes_from_context())))
            yield event()

    with tracer.start_as_current_span("consumer") as parent:
        streams_to_close = [wrap(source, "one"), wrap(source, "two")]
        for stream in streams_to_close * 2:
            await next_event(stream)
            assert trace.get_current_span() is parent
        for stream in streams_to_close:
            await stream.aclose()
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert all(s.parent == parent.get_span_context() for s in spans)
        if kind == "runner":
            assert all(
                attrs[SpanAttributes.SESSION_ID] == kwargs["session_id"]
                for kwargs, attrs in observed
            )


@pytest.mark.asyncio
async def test_suppressed_stream_is_unchanged(streams):
    wrap, exporter, _, _ = streams

    async def source(**kwargs):
        yield event()

    original = source()
    token = context_api.attach(
        context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        stream = wrap(lambda **kwargs: original)
        assert stream is original
        assert len([e async for e in stream]) == 1
    finally:
        context_api.detach(token)
    assert not exporter.get_finished_spans()
