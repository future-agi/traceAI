"""Context-helper decorators preserve attribution for the executed body."""

import asyncio
import inspect

import pytest
from opentelemetry.context import Context, attach, detach, get_current
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.context_attributes import (
    get_attributes_from_context,
    using_attributes,
    using_metadata,
    using_prompt_template,
    using_session,
    using_tags,
    using_user,
)


SESSION = {
    SpanAttributes.SESSION_ID: "decorated-session",
    SpanAttributes.GEN_AI_CONVERSATION_ID: "decorated-session",
}
USER = {SpanAttributes.USER_ID: "decorated-user"}
METADATA = {SpanAttributes.METADATA: '{"source": "test"}'}
TAGS = {SpanAttributes.TAG_TAGS: ("first", "second")}
PROMPT = {
    SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME: "Hello {name}",
    SpanAttributes.GEN_AI_PROMPT_TEMPLATE_LABEL: "test",
    SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION: "v1",
    SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES: '{"name": "reader"}',
}
HELPERS = [
    pytest.param(lambda: using_session("decorated-session"), SESSION, id="session"),
    pytest.param(lambda: using_user("decorated-user"), USER, id="user"),
    pytest.param(lambda: using_metadata({"source": "test"}), METADATA, id="metadata"),
    pytest.param(lambda: using_tags(["first", "second"]), TAGS, id="tags"),
    pytest.param(
        lambda: using_prompt_template(
            template="Hello {name}", label="test", version="v1",
            variables={"name": "reader"},
        ),
        PROMPT,
        id="prompt-template",
    ),
    pytest.param(
        lambda: using_attributes(
            session_id="decorated-session", user_id="decorated-user",
            metadata={"source": "test"}, tags=["first", "second"],
            prompt_template="Hello {name}", prompt_template_label="test",
            prompt_template_version="v1", prompt_template_variables={"name": "reader"},
        ),
        {**SESSION, **USER, **METADATA, **TAGS, **PROMPT},
        id="attributes",
    ),
]


@pytest.fixture
def tracing():
    token = attach(Context())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("context-helper-tests"))
    try:
        yield tracer, exporter
    finally:
        provider.shutdown()
        detach(token)


def span_attributes(exporter):
    return {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}


def assert_attributes(actual, expected):
    for key, value in expected.items():
        assert actual.get(key) == value, key


@pytest.mark.parametrize("helper,expected", HELPERS)
def test_async_decorator_attributes_after_await(tracing, helper, expected):
    tracer, exporter = tracing

    @helper()
    async def decorated(value, *, suffix):
        """Return a value after recording the attributed span."""
        await asyncio.sleep(0)
        with tracer.start_as_current_span("body"):
            return value + suffix

    async def run():
        prior = get_current()
        pending = decorated("result", suffix="!")
        assert get_current() is prior
        assert await pending == "result!"
        assert get_current() is prior

    asyncio.run(run())
    assert_attributes(span_attributes(exporter)["body"], expected)
    assert inspect.iscoroutinefunction(decorated)
    assert decorated.__name__ == "decorated"
    assert decorated.__doc__ == "Return a value after recording the attributed span."
    assert tuple(inspect.signature(decorated).parameters) == ("value", "suffix")


def test_async_decorator_restores_context_after_exception(tracing):
    tracer, exporter = tracing
    error = ValueError("expected failure")

    @using_session("inner-session")
    async def decorated():
        await asyncio.sleep(0)
        with tracer.start_as_current_span("exception"):
            raise error

    async def run():
        with using_session("outer-session"):
            prior = get_current()
            with pytest.raises(ValueError) as caught:
                await decorated()
            assert caught.value is error
            assert get_current() is prior
        assert dict(get_attributes_from_context()) == {}

    asyncio.run(run())
    assert span_attributes(exporter)["exception"][SpanAttributes.SESSION_ID] == "inner-session"


def test_concurrent_decorators_keep_distinct_sessions(tracing):
    tracer, exporter = tracing

    async def run():
        ready = [asyncio.Event(), asyncio.Event()]

        def task(index, session):
            @using_session(session)
            async def decorated():
                ready[index].set()
                await ready[1 - index].wait()
                await asyncio.sleep(0)
                with tracer.start_as_current_span(session):
                    pass
            return decorated()

        prior = get_current()
        await asyncio.gather(task(0, "first-session"), task(1, "second-session"))
        assert get_current() is prior

    asyncio.run(run())
    spans = span_attributes(exporter)
    for session in ("first-session", "second-session"):
        assert spans[session][SpanAttributes.SESSION_ID] == session
        assert spans[session][SpanAttributes.GEN_AI_CONVERSATION_ID] == session


def test_shared_async_decorator_uses_call_local_tokens(tracing):
    tracer, exporter = tracing

    async def run():
        ready = [asyncio.Event(), asyncio.Event()]
        first_finished = asyncio.Event()

        @using_user("shared-user")
        async def decorated(index):
            ready[index].set()
            await ready[1 - index].wait()
            if index == 1:
                await first_finished.wait()
            with tracer.start_as_current_span(str(index)):
                pass

        async def task(index):
            with using_attributes(session_id=str(index), user_id="outer-user"):
                prior = get_current()
                try:
                    await decorated(index)
                    assert get_current() is prior
                finally:
                    if index == 0:
                        first_finished.set()

        await asyncio.gather(task(0), task(1))
        assert dict(get_attributes_from_context()) == {}

    asyncio.run(run())
    spans = span_attributes(exporter)
    for index in ("0", "1"):
        assert spans[index][SpanAttributes.SESSION_ID] == index
        assert spans[index][SpanAttributes.USER_ID] == "shared-user"


def test_recursive_async_decorator_restores_each_call(tracing):
    tracer, exporter = tracing

    @using_session("recursive-session")
    async def decorated(depth):
        await asyncio.sleep(0)
        if depth:
            prior = get_current()
            await decorated(depth - 1)
            assert get_current() is prior
        with tracer.start_as_current_span(str(depth)):
            pass

    async def run():
        with using_session("outer-session"):
            prior = get_current()
            await decorated(2)
            assert get_current() is prior

    asyncio.run(run())
    for attributes in span_attributes(exporter).values():
        assert attributes[SpanAttributes.SESSION_ID] == "recursive-session"


def test_recursive_sync_decorator_restores_each_call(tracing):
    tracer, exporter = tracing

    @using_session("recursive-session")
    def decorated(depth):
        if depth:
            prior = get_current()
            decorated(depth - 1)
            assert get_current() is prior
        with tracer.start_as_current_span(str(depth)):
            pass

    with using_session("outer-session"):
        prior = get_current()
        decorated(2)
        assert get_current() is prior
    for attributes in span_attributes(exporter).values():
        assert attributes[SpanAttributes.SESSION_ID] == "recursive-session"


def test_async_decorator_restores_context_after_cancellation(tracing):
    tracer, exporter = tracing

    async def run():
        started = asyncio.Event()
        pending = asyncio.Event()

        @using_session("cancelled-session")
        async def decorated():
            await asyncio.sleep(0)
            with tracer.start_as_current_span("cancelled"):
                started.set()
                await pending.wait()

        async def caller():
            with using_session("outer-session"):
                prior = get_current()
                try:
                    await decorated()
                finally:
                    assert get_current() is prior

        task = asyncio.create_task(caller())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert dict(get_attributes_from_context()) == {}

    asyncio.run(run())
    assert span_attributes(exporter)["cancelled"][SpanAttributes.SESSION_ID] == "cancelled-session"


@pytest.mark.parametrize("ending", ["exhaust", "close", "exception"])
def test_async_generator_keeps_attribution_across_yields(tracing, ending):
    tracer, exporter = tracing
    cleaned_up = []

    @using_session("generator-session")
    async def decorated():
        """Yield attributed results."""
        try:
            for index in range(2):
                await asyncio.sleep(0)
                with tracer.start_as_current_span(str(index)):
                    pass
                yield index
            if ending == "exception":
                raise ValueError("generator failure")
        finally:
            cleaned_up.append(True)

    async def run():
        with using_session("outer-session"):
            prior = get_current()
            iterator = decorated()
            assert get_current() is prior
            try:
                assert await iterator.__anext__() == 0
                await asyncio.sleep(0)
                assert await iterator.__anext__() == 1
                if ending == "close":
                    await iterator.aclose()
                elif ending == "exception":
                    with pytest.raises(ValueError, match="generator failure"):
                        await iterator.__anext__()
                else:
                    with pytest.raises(StopAsyncIteration):
                        await iterator.__anext__()
            finally:
                await iterator.aclose()
            assert get_current() is prior
            assert cleaned_up == [True]

    asyncio.run(run())
    assert len(span_attributes(exporter)) == 2
    for attributes in span_attributes(exporter).values():
        assert attributes[SpanAttributes.SESSION_ID] == "generator-session"
    assert inspect.isasyncgenfunction(decorated)
    assert decorated.__name__ == "decorated"
    assert decorated.__doc__ == "Yield attributed results."


@pytest.mark.parametrize("ending", ["exhaust", "close-out-of-order"])
@pytest.mark.parametrize("stacked", [False, True], ids=["combined", "stacked"])
def test_interleaved_async_generators_isolate_each_resume(tracing, ending, stacked):
    tracer, exporter = tracing

    def stream(name):
        async def body():
            with using_tags([name]):
                try:
                    for index in range(3):
                        await asyncio.sleep(0)
                        with tracer.start_as_current_span(f"{name}-{index}"):
                            pass
                        yield index
                finally:
                    await asyncio.sleep(0)
                    with tracer.start_as_current_span(f"{name}-cleanup"):
                        pass

        if stacked:
            return using_session(name)(using_user(name + "-user")(body))()
        return using_attributes(session_id=name, user_id=name + "-user")(body)()

    async def run():
        with using_attributes(session_id="outer-session", user_id="outer-user"):
            prior = get_current()
            contexts = []
            first, second = stream("first"), stream("second")

            async def advance(iterator, expected):
                assert await iterator.__anext__() == expected
                contexts.append(get_current())
                with tracer.start_as_current_span(f"caller-{len(contexts)}"):
                    pass

            try:
                await advance(first, 0)
                await advance(second, 0)
                await advance(first, 1)
                if ending == "close-out-of-order":
                    await first.aclose()
                    contexts.append(get_current())
                    await advance(second, 1)
                    await second.aclose()
                    contexts.append(get_current())
                else:
                    await advance(second, 1)
                    await advance(first, 2)
                    await advance(second, 2)
                    for iterator in (first, second):
                        with pytest.raises(StopAsyncIteration):
                            await iterator.__anext__()
                        contexts.append(get_current())
            finally:
                await first.aclose()
                await second.aclose()
            contexts.append(get_current())
            return prior, contexts

    prior, contexts = asyncio.run(run())
    spans = span_attributes(exporter)
    count = 3 if ending == "exhaust" else 2
    for name in ("first", "second"):
        for suffix in [*range(count), "cleanup"]:
            assert_attributes(spans[f"{name}-{suffix}"], {
                SpanAttributes.SESSION_ID: name,
                SpanAttributes.GEN_AI_CONVERSATION_ID: name,
                SpanAttributes.USER_ID: name + "-user",
                SpanAttributes.TAG_TAGS: (name,),
            })
    for name, attributes in spans.items():
        if name.startswith("caller-"):
            assert_attributes(attributes, {
                SpanAttributes.SESSION_ID: "outer-session",
                SpanAttributes.USER_ID: "outer-user",
            })
            assert SpanAttributes.TAG_TAGS not in attributes
    assert all(context is prior for context in contexts)


def test_async_generator_asend_preserves_body_context_and_restores_each_caller(tracing):
    tracer, exporter = tracing
    received = []

    @using_session("generator-session")
    async def decorated():
        with using_user("body-user"):
            try:
                received.append((yield "ready"))
                await asyncio.sleep(0)
                with tracer.start_as_current_span("sent"):
                    pass
                received.append((yield "sent"))
                with tracer.start_as_current_span("finished"):
                    pass
            finally:
                with tracer.start_as_current_span("cleanup"):
                    pass

    async def run():
        with using_attributes(session_id="outer-session", user_id="outer-user"):
            original = get_current()
            iterator = decorated()
            contexts = []
            try:
                assert await iterator.__anext__() == "ready"
                contexts.append((get_current(), original))
                with using_attributes(session_id="changed-session", user_id="changed-user"):
                    changed = get_current()
                    assert await iterator.asend("payload") == "sent"
                    contexts.append((get_current(), changed))
                with pytest.raises(StopAsyncIteration):
                    await iterator.asend("last")
                contexts.append((get_current(), original))
            finally:
                await iterator.aclose()
            assert received == ["payload", "last"]
            assert all(actual is expected for actual, expected in contexts)

    asyncio.run(run())
    spans = span_attributes(exporter)
    assert set(spans) == {"sent", "finished", "cleanup"}
    for attributes in spans.values():
        assert_attributes(attributes, {
            SpanAttributes.SESSION_ID: "generator-session",
            SpanAttributes.USER_ID: "body-user",
        })


@pytest.mark.parametrize("handled", [False, True], ids=["propagated", "handled"])
def test_async_generator_athrow_runs_body_and_cleanup_in_its_context(tracing, handled):
    tracer, exporter = tracing
    error = ValueError("injected failure")
    caught = []

    @using_attributes(session_id="generator-session", user_id="generator-user")
    async def decorated():
        try:
            try:
                yield "ready"
            except ValueError as exc:
                caught.append(exc)
                await asyncio.sleep(0)
                with tracer.start_as_current_span("caught"):
                    pass
                if not handled:
                    raise
                yield "recovered"
        finally:
            await asyncio.sleep(0)
            with tracer.start_as_current_span("cleanup"):
                pass

    async def run():
        with using_attributes(session_id="outer-session", user_id="outer-user"):
            prior = get_current()
            contexts = []
            iterator = decorated()
            try:
                assert await iterator.__anext__() == "ready"
                contexts.append(get_current())
                if handled:
                    assert await iterator.athrow(error) == "recovered"
                    contexts.append(get_current())
                    await iterator.aclose()
                else:
                    with pytest.raises(ValueError) as raised:
                        await iterator.athrow(error)
                    assert raised.value is error
                contexts.append(get_current())
            finally:
                await iterator.aclose()
            assert caught == [error]
            assert all(context is prior for context in contexts)

    asyncio.run(run())
    spans = span_attributes(exporter)
    assert set(spans) == {"caught", "cleanup"}
    for attributes in spans.values():
        assert_attributes(attributes, {
            SpanAttributes.SESSION_ID: "generator-session",
            SpanAttributes.USER_ID: "generator-user",
        })


def test_async_generator_cancelled_resume_restores_caller_context(tracing):
    tracer, exporter = tracing

    async def run():
        started = asyncio.Event()
        pending = asyncio.Event()

        @using_session("generator-session")
        async def decorated():
            try:
                yield "ready"
                started.set()
                await pending.wait()
            finally:
                with tracer.start_as_current_span("cleanup"):
                    pass

        async def caller():
            with using_session("outer-session"):
                prior = get_current()
                iterator = decorated()
                after_yield = prior
                try:
                    assert await iterator.__anext__() == "ready"
                    after_yield = get_current()
                    await iterator.__anext__()
                finally:
                    await iterator.aclose()
                    assert get_current() is prior
                    assert after_yield is prior

        task = asyncio.create_task(caller())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert span_attributes(exporter)["cleanup"][SpanAttributes.SESSION_ID] == "generator-session"


@pytest.mark.parametrize("helper,expected", HELPERS)
@pytest.mark.parametrize("mode", ["sync-decorator", "with-block", "async-with-block"])
def test_control_existing_context_forms(tracing, helper, expected, mode):
    tracer, exporter = tracing
    prior = get_current()

    def body():
        with tracer.start_as_current_span("control"):
            return "result"

    if mode == "sync-decorator":
        decorated = helper()(body)
        assert decorated() == "result"
        assert decorated.__name__ == body.__name__
    elif mode == "with-block":
        with helper():
            assert body() == "result"
    else:
        async def run():
            before = get_current()
            async with helper():
                await asyncio.sleep(0)
                assert body() == "result"
            assert get_current() is before
        asyncio.run(run())

    assert get_current() is prior
    assert_attributes(span_attributes(exporter)["control"], expected)
