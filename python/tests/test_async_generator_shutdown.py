"""The tracing wrapper owns async-generator cleanup, including loop shutdown."""

import asyncio
import gc
import sys
from contextvars import ContextVar

import pytest
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, get_current_span

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig, suppress_tracing


@pytest.fixture(params=["chain", "agent", "tool"])
def tracing(request):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig())
    yield tracer, getattr(tracer, request.param), exporter
    provider.shutdown()


@pytest.mark.parametrize("instrumented", [False, True])
@pytest.mark.parametrize("explicit_shutdown", [False, True])
def test_shutdown_has_one_finalizer(tracing, instrumented, explicit_shutdown):
    _, decorate, exporter = tracing
    # Keep references alive across asyncio.run so shutdown_asyncgens, rather
    # than reference-count finalization, owns the close. Repeat to vary set order.
    retained = []
    for _ in range(12):
        exporter.clear()
        errors = []
        cleanup = []

        async def stream():
            try:
                yield "ready"
            finally:
                await asyncio.sleep(0.001)
                cleanup.append(True)

        factory = decorate(stream) if instrumented else stream

        async def consume():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda loop, context: errors.append(context))
            iterator = factory()
            assert await iterator.__anext__() == "ready"
            if explicit_shutdown:
                await loop.shutdown_asyncgens()
                with pytest.raises(StopAsyncIteration):
                    await iterator.__anext__()
            return iterator

        retained.append(asyncio.run(consume()))
        assert cleanup == [True]
        assert not errors, [repr(context.get("exception")) for context in errors]
        spans = exporter.get_finished_spans()
        assert len(spans) == int(instrumented)
        assert all(span.status.status_code is StatusCode.UNSET for span in spans)
        assert all(not span.events for span in spans)


@pytest.mark.parametrize("garbage_collected", [False, True])
def test_shutdown_awaits_cleanup_in_generator_context(
    tracing, garbage_collected, caplog
):
    tracer, decorate, exporter = tracing
    marker = ContextVar("shutdown-marker", default="consumer")
    errors = []
    observations = []
    completed = None

    @decorate
    def private_step(value):
        return value

    @decorate
    async def stream():
        span = get_current_span()
        token = marker.set("generator")
        try:
            with tracer.start_as_current_span("child") as child, suppress_tracing():
                try:
                    yield "ready"
                finally:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0.001)
                    observations.append(
                        (
                            span.is_recording(),
                            get_current_span() is child,
                            marker.get(),
                            bool(get_value(_SUPPRESS_INSTRUMENTATION_KEY)),
                            len(exporter.get_finished_spans()),
                        )
                    )
                    private_step("SYNTHETIC_PRIVATE_CLEANUP")
        finally:
            marker.reset(token)
            completed.set()

    async def consume():
        nonlocal completed
        completed = asyncio.Event()
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: errors.append(context)
        )
        iterator = stream()
        assert await iterator.__anext__() == "ready"
        assert marker.get() == "consumer"
        assert not get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        if garbage_collected:
            del iterator
            gc.collect()
            await asyncio.wait_for(completed.wait(), timeout=5)
            return None
        return iterator

    retained = asyncio.run(consume())
    assert (retained is None) == garbage_collected
    assert not errors, [repr(context.get("exception")) for context in errors]
    assert observations == [(True, True, "generator", True, 0)]
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["child", "stream"]
    assert spans[0].parent.span_id == spans[1].context.span_id
    assert spans[0].end_time <= spans[1].end_time
    assert all(span.status.status_code is StatusCode.UNSET for span in spans)
    assert "Failed to detach context" not in caplog.text


def test_shutdown_still_reports_real_cleanup_failure(tracing):
    _, decorate, exporter = tracing
    errors = []
    failure = ValueError("synthetic cleanup failure")

    @decorate
    async def stream():
        try:
            yield "ready"
        finally:
            await asyncio.sleep(0.001)
            raise failure

    async def consume():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: errors.append(context)
        )
        iterator = stream()
        await iterator.__anext__()
        return iterator

    retained = asyncio.run(consume())
    assert retained.ag_frame is None
    assert [context.get("exception") for context in errors] == [failure]
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1
    assert span.attributes["error.type"].endswith("ValueError")


def test_only_wrapped_generator_skips_loop_registration(tracing):
    _, decorate, exporter = tracing
    registered = []
    cleanup = []
    nested = []

    async def unrelated():
        try:
            yield "nested"
        finally:
            await asyncio.sleep(0.001)
            cleanup.append("nested")

    async def stream():
        iterator = unrelated()
        nested.append(iterator)
        assert await iterator.__anext__() == "nested"
        try:
            yield "ready"
        finally:
            await asyncio.sleep(0.001)
            cleanup.append("stream")

    decorated = decorate(stream)

    async def consume():
        hooks = sys.get_asyncgen_hooks()

        def firstiter(generator):
            registered.append(generator)
            if hooks.firstiter is not None:
                hooks.firstiter(generator)

        sys.set_asyncgen_hooks(firstiter=firstiter)
        expected_hooks = sys.get_asyncgen_hooks()
        try:
            iterator = decorated()
            assert await iterator.__anext__() == "ready"
            assert sys.get_asyncgen_hooks() == expected_hooks
            return iterator
        finally:
            sys.set_asyncgen_hooks(*hooks)

    retained = asyncio.run(consume())
    assert retained in registered
    assert nested[0] in registered
    assert all(generator.ag_code is not stream.__code__ for generator in registered)
    assert sorted(cleanup) == ["nested", "stream"]
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET
