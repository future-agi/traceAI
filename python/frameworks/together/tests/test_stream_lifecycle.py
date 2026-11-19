"""Streaming calls keep the SDK's stream object usable and end their span exactly once."""
import asyncio
import json

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_together import TogetherInstrumentor


def _resource(*candidates):
    """The first (module, class) the installed SDK has: the 1.x layout, then the 3.x one."""
    import importlib

    for module, name in candidates:
        try:
            return getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError):
            continue
    pytest.skip("no matching resource class in the installed together SDK")


def _classes(kind):
    if kind == "chat":
        return (_resource(("together.resources.chat", "ChatCompletions"), ("together.resources.chat.completions", "CompletionsResource")),
                _resource(("together.resources.chat", "AsyncChatCompletions"), ("together.resources.chat.completions", "AsyncCompletionsResource")))
    return (_resource(("together.resources.completions", "Completions"), ("together.resources.completions", "CompletionsResource")),
            _resource(("together.resources.completions", "AsyncCompletions"), ("together.resources.completions", "AsyncCompletionsResource")))


def _chunks(kind, fail_after=None):
    out = []
    for i, word in enumerate(["hello", " world"]):
        choice = {"index": 0, "delta": {"content": word}} if kind == "chat" else {"index": 0, "text": word}
        out.append({"id": "c1", "model": "m", "choices": [choice]})
    out.append({"id": "c1", "model": "m", "choices": [{"index": 0, "finish_reason": "stop",
                                                       **({"delta": {}} if kind == "chat" else {"text": ""})}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})
    return out


class FakeStream:
    """The shape of the SDK's stream object: iteration, a context manager, close and its own attributes."""

    sdk_attribute = "sdk-value"

    def __init__(self, chunks, fail_at=None):
        self._chunks = list(chunks)
        self._index = 0
        self._fail_at = fail_at
        self.closed = False
        self.entered = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._fail_at is not None and self._index == self._fail_at:
            raise RuntimeError("synthetic stream failure")
        if self._index >= len(self._chunks):
            raise StopIteration
        self._index += 1
        return self._chunks[self._index - 1]

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True


class FakeAsyncStream:
    sdk_attribute = "sdk-value"

    def __init__(self, chunks, fail_at=None):
        self._chunks = list(chunks)
        self._index = 0
        self._fail_at = fail_at
        self.closed = False
        self.entered = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._fail_at is not None and self._index == self._fail_at:
            raise RuntimeError("synthetic stream failure")
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        self._index += 1
        return self._chunks[self._index - 1]

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        self.closed = True

    close = aclose


@pytest.fixture(params=["chat", "completions"])
def kind(request):
    return request.param


@pytest.fixture(params=[TraceConfig(), TraceConfig(hide_inputs=True), TraceConfig(hide_input_messages=True)],
                ids=["default", "hide_inputs", "hide_input_messages"])
def config(request):
    return request.param


@pytest.fixture
def traced(kind, config):
    sync_cls, async_cls = _classes(kind)
    created = {}

    def sync_create(self, *args, **kwargs):
        created["stream"] = FakeStream(_chunks(kind), fail_at=created.get("fail_at"))
        return created["stream"]

    async def async_create(self, *args, **kwargs):
        created["stream"] = FakeAsyncStream(_chunks(kind), fail_at=created.get("fail_at"))
        return created["stream"]

    original = (sync_cls.create, async_cls.create)
    sync_cls.create, async_cls.create = sync_create, async_create
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        yield sync_cls, async_cls, created, exporter
    finally:
        instrumentor.uninstrument()
        sync_cls.create, async_cls.create = original


def _call(kind, sync_cls):
    args = {"messages": [{"role": "user", "content": "hi"}]} if kind == "chat" else {"prompt": "hi"}
    return sync_cls.__new__(sync_cls).create(model="m", stream=True, **args)


async def _acall(kind, async_cls):
    args = {"messages": [{"role": "user", "content": "hi"}]} if kind == "chat" else {"prompt": "hi"}
    return await async_cls.__new__(async_cls).create(model="m", stream=True, **args)


def _only(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


def test_a_sync_stream_keeps_the_sdk_object_and_ends_its_span_once_when_exhausted(traced, kind):
    sync_cls, _, created, exporter = traced
    stream = _call(kind, sync_cls)
    assert stream.sdk_attribute == "sdk-value"
    assert isinstance(stream, FakeStream)
    assert len(list(stream)) == 3
    span = _only(exporter)
    assert "hello world" in str(dict(span.attributes).get("output.value"))
    assert "together.stream.incomplete" not in span.attributes


def test_a_sync_stream_works_as_a_context_manager(traced, kind):
    sync_cls, _, created, exporter = traced
    with _call(kind, sync_cls) as stream:
        assert created["stream"].entered
        assert len(list(stream)) == 3
    assert created["stream"].closed
    _only(exporter)


def test_breaking_out_and_closing_ends_the_span_once_and_marks_it_incomplete(traced, kind):
    sync_cls, _, created, exporter = traced
    stream = _call(kind, sync_cls)
    for _ in stream:
        break
    stream.close()
    stream.close()
    assert created["stream"].closed
    span = _only(exporter)
    assert span.attributes["together.stream.incomplete"] is True


def test_breaking_out_of_a_with_block_ends_the_span_once_and_marks_it_incomplete(traced, kind):
    sync_cls, _, created, exporter = traced
    with _call(kind, sync_cls) as stream:
        for _ in stream:
            break
    assert created["stream"].closed
    assert _only(exporter).attributes["together.stream.incomplete"] is True


def test_a_stream_that_is_dropped_after_an_early_break_still_ends_its_span_once(traced, kind):
    import gc

    sync_cls, _, created, exporter = traced
    stream = _call(kind, sync_cls)
    for _ in stream:
        break
    del stream
    gc.collect()
    assert _only(exporter).attributes["together.stream.incomplete"] is True


def test_a_failing_sync_stream_marks_the_span_as_an_error_once(traced, kind):
    sync_cls, _, created, exporter = traced
    created["fail_at"] = 1
    stream = _call(kind, sync_cls)
    with pytest.raises(RuntimeError):
        list(stream)
    span = _only(exporter)
    assert span.status.status_code.name == "ERROR"


def test_an_async_stream_is_returned_ready_to_iterate_and_ends_its_span_once(traced, kind):
    _, async_cls, created, exporter = traced

    async def run():
        stream = await _acall(kind, async_cls)
        assert stream.sdk_attribute == "sdk-value"
        return [chunk async for chunk in stream]

    assert len(asyncio.run(run())) == 3
    span = _only(exporter)
    assert "hello world" in str(dict(span.attributes).get("output.value"))


def test_an_async_stream_works_as_an_async_context_manager_and_closes(traced, kind):
    _, async_cls, created, exporter = traced

    async def run():
        async with await _acall(kind, async_cls) as stream:
            assert created["stream"].entered
            async for _ in stream:
                break

    asyncio.run(run())
    assert created["stream"].closed
    assert _only(exporter).attributes["together.stream.incomplete"] is True


def test_an_async_stream_closed_after_an_early_break_ends_once_and_is_incomplete(traced, kind):
    _, async_cls, created, exporter = traced

    async def run():
        stream = await _acall(kind, async_cls)
        async for _ in stream:
            break
        await stream.aclose()
        await stream.aclose()

    asyncio.run(run())
    assert _only(exporter).attributes["together.stream.incomplete"] is True


def test_a_failing_async_stream_marks_the_span_as_an_error_once(traced, kind):
    _, async_cls, created, exporter = traced
    created["fail_at"] = 1

    async def run():
        stream = await _acall(kind, async_cls)
        async for _ in stream:
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert _only(exporter).status.status_code.name == "ERROR"


def _exception_types(span):
    return [event.attributes.get("exception.type") for event in span.events if event.name == "exception"]


def test_an_exception_raised_inside_a_with_block_ends_the_span_with_an_error(traced, kind):
    sync_cls, _, created, exporter = traced
    with pytest.raises(ValueError):
        with _call(kind, sync_cls) as stream:
            next(iter(stream))
            raise ValueError("synthetic caller failure")
    assert created["stream"].closed
    span = _only(exporter)
    assert span.status.status_code.name == "ERROR"
    assert _exception_types(span) == ["ValueError"]


def test_an_exception_raised_inside_an_async_with_block_ends_the_span_with_an_error(traced, kind):
    _, async_cls, created, exporter = traced

    async def run():
        async with await _acall(kind, async_cls) as stream:
            async for _ in stream:
                break
            raise ValueError("synthetic caller failure")

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert created["stream"].closed
    span = _only(exporter)
    assert span.status.status_code.name == "ERROR"
    assert _exception_types(span) == ["ValueError"]
