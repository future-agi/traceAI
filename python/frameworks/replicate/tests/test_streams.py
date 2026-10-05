"""Client.stream and async_stream: one span per stream (PRD J4)."""

from __future__ import annotations

import asyncio
import gc
import inspect
import types

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _support import (  # noqa: E402
    PROMPT,
    STREAM_CHUNKS,
    STREAM_ERROR,
    STREAM_ERROR_MODEL,
    STREAM_FILE_MODEL,
    STREAM_MODEL,
    STREAM_SLOW_MODEL,
    STREAM_TEXT,
    FakeReplicate,
    RecordingTransport,
    attrs,
    exception_events,
    instrumented,
    make_client,
)


def _texts(events):
    return [str(event) for event in events if str(event)]


def _assert_cancelled_once(traced):
    spans = traced.replicate_spans()
    assert len(spans) == 1, [span.name for span in spans]
    span = spans[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert attrs(span)["replicate.cancelled"] is True
    assert exception_events(span) == []
    return span


def test_stream_is_one_llm_span_with_the_concatenated_text():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        client = make_client(fake, transport)
        stream = client.stream(STREAM_MODEL, input={"prompt": PROMPT})
        assert isinstance(stream, types.GeneratorType)  # still the vendor's type
        events = list(stream)

    assert _texts(events) == STREAM_CHUNKS
    span = traced.one()
    assert span.name == "replicate.stream"
    values = attrs(span)
    assert values["gen_ai.span.kind"] == "LLM"
    assert values["gen_ai.request.model"] == STREAM_MODEL
    assert values["replicate.prediction.id"] == "pred0001"
    assert values["output.value"] == STREAM_TEXT
    assert values["replicate.output.type"] == "text"
    assert span.status.status_code is StatusCode.OK
    # The create POST and the SSE GET nest under the stream span.
    http = [s for s in traced.spans() if s.name.startswith("HTTP ")]
    assert [s.name for s in http] == ["HTTP POST", "HTTP GET"]
    assert {s.parent.span_id for s in http} == {span.context.span_id}


def test_stream_span_stays_open_until_the_stream_ends():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        stream = client.stream(STREAM_MODEL, input={})
        next(stream)
        assert traced.replicate_spans() == []
        list(stream)
        assert len(traced.replicate_spans()) == 1


def test_closing_a_stream_early_ends_its_span_once_as_cancelled():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        stream = client.stream(STREAM_MODEL, input={})
        next(stream)
        stream.close()
        _assert_cancelled_once(traced)  # asserted while the stream is still held
        del stream
        gc.collect()
        _assert_cancelled_once(traced)


def test_dropping_a_stream_mid_iteration_ends_its_span_as_cancelled():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        stream = client.stream(STREAM_MODEL, input={})
        next(stream)
        del stream
        gc.collect()
        _assert_cancelled_once(traced)


def test_stream_error_event_raises_and_marks_the_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        with pytest.raises(RuntimeError, match=STREAM_ERROR):
            list(client.stream(STREAM_ERROR_MODEL, input={}))

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    (event,) = exception_events(span)
    assert event.attributes["exception.type"] == "RuntimeError"


def test_file_events_are_counted_not_inlined_and_never_fetched():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport()
        client = make_client(fake, transport)
        events = list(client.stream(STREAM_FILE_MODEL, input={}))

    assert len(events) == 3
    values = attrs(traced.one())
    assert values["replicate.stream.file_count"] == 2
    assert "output.value" not in values
    assert values["gen_ai.span.kind"] == "CHAIN"
    assert "OUTPUT-FILE-MARKER" not in traced.wire()
    assert not [url for _, url in transport.requests if "files.example.invalid" in url]


def test_stream_text_is_redacted_when_outputs_are_hidden():
    with FakeReplicate() as fake, instrumented(config=TraceConfig(hide_outputs=True)) as traced:
        client = make_client(fake)
        list(client.stream(STREAM_MODEL, input={}))

    assert attrs(traced.one())["output.value"] == "__REDACTED__"
    assert STREAM_TEXT not in traced.wire()


def test_async_stream_parity():
    async def consume(client):
        stream = await client.async_stream(STREAM_MODEL, input={"prompt": PROMPT})
        assert inspect.isasyncgen(stream)
        return [event async for event in stream]

    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        client = make_client(fake, transport)
        events = asyncio.run(consume(client))

    assert _texts(events) == STREAM_CHUNKS
    span = traced.one()
    assert span.name == "replicate.stream"
    assert attrs(span)["output.value"] == STREAM_TEXT
    assert span.status.status_code is StatusCode.OK
    http = [s for s in traced.spans() if s.name.startswith("HTTP ")]
    assert {s.parent.span_id for s in http} == {span.context.span_id}


def test_async_stream_aclose_ends_its_span_once_as_cancelled():
    async def consume_one(client, traced):
        stream = await client.async_stream(STREAM_MODEL, input={})
        await stream.__anext__()
        await stream.aclose()
        _assert_cancelled_once(traced)

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(consume_one(make_client(fake), traced))
        _assert_cancelled_once(traced)


def test_cancelling_a_task_reading_an_async_stream_ends_its_span_as_cancelled():
    async def reader(client):
        stream = await client.async_stream(STREAM_SLOW_MODEL, input={})
        async for _ in stream:
            pass

    async def main(client):
        task = asyncio.ensure_future(reader(client))
        await asyncio.sleep(0.3)  # first event read; the fake now stalls
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(main(make_client(fake)))
        _assert_cancelled_once(traced)
