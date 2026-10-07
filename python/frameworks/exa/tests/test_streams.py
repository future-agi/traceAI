"""Stream lifecycle from the real SDK against the loopback fake's SSE routes."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest
from opentelemetry.trace import StatusCode

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import (  # noqa: E402
    CONTENT_MARKERS,
    EXA_KEY,
    FAIL_QUERY,
    SLOW_QUERY,
    STREAM_CHUNKS,
    STREAM_CITATIONS,
    FakeExa,
    attrs,
    instrumented,
)
from exa_py import AsyncExa, Exa  # noqa: E402
from exa_py.api import (  # noqa: E402
    AsyncStreamAnswerResponse,
    AsyncStreamSearchResponse,
    StreamAnswerResponse,
    StreamSearchResponse,
)


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def test_sync_streams_keep_the_vendor_type(fake):
    with instrumented():
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        search = client.stream_search("q")
        answer = client.stream_answer("q")
        assert isinstance(search, StreamSearchResponse)
        assert isinstance(answer, StreamAnswerResponse)
        list(search)
        list(answer)


def test_async_streams_keep_the_vendor_type(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            search = await client.stream_search("q")
            answer = await client.stream_answer("q")
            assert isinstance(search, AsyncStreamSearchResponse)
            assert isinstance(answer, AsyncStreamAnswerResponse)
            async for _ in search:
                pass
            async for _ in answer:
                pass
        finally:
            await client.client.aclose()

    with instrumented():
        asyncio.run(call())


def _assert_cancelled(span) -> None:
    values = attrs(span)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert values["exa.cancelled"] is True
    # A cancelled stream's total is unknown, and cancelling is not an exception.
    assert "fi.retrieval.document_count" not in values
    assert not [event for event in span.events if event.name == "exception"]


async def _until(predicate) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_sync_stream_closed_after_one_chunk_is_cancelled(fake):
    with instrumented() as traced:
        stream = Exa(api_key=EXA_KEY, base_url=fake.origin).stream_search("q")
        next(iter(stream))
        stream.close()
        _assert_cancelled(traced.one())


def test_sync_stream_dropped_mid_iteration_is_cancelled_on_gc(fake):
    with instrumented() as traced:
        stream = Exa(api_key=EXA_KEY, base_url=fake.origin).stream_answer("q")
        next(iter(stream))
        assert traced.spans() == []
        del stream
        gc.collect()
        _assert_cancelled(traced.one())


def test_async_stream_aclosed_after_one_chunk_is_cancelled(fake):
    async def call(traced) -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            stream = await client.stream_answer("q")
            await stream.__anext__()
            await stream.aclose()
            _assert_cancelled(traced.one())
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call(traced))


def test_async_stream_dropped_mid_iteration_is_cancelled_on_gc(fake):
    async def call(traced) -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            stream = await client.stream_search("q")
            await stream.__anext__()
            raw = stream._raw_response
            del stream
            gc.collect()
            _assert_cancelled(traced.one())
            await raw.aclose()
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call(traced))


def test_cancelled_async_call_is_marked_cancelled(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            task = asyncio.ensure_future(client.search(SLOW_QUERY))
            await _until(lambda: fake.calls)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    _assert_cancelled(traced.one())


def test_async_stream_cancelled_mid_chunk_is_marked_cancelled(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            stream = await client.stream_answer(SLOW_QUERY)
            await stream.__anext__()  # the fake now stalls before chunk two
            task = asyncio.ensure_future(stream.__anext__())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await stream.aclose()  # already ended: must not end it again
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    _assert_cancelled(traced.one())


async def _close_after_one_chunk(fake, after_close=lambda: None) -> type:
    """Call close() on an async stream after one chunk; return what it raised.

    ``after_close`` runs while the stream is still referenced, so a span ended
    there was ended by close() and not by garbage collection.
    """
    client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
    try:
        stream = await client.stream_search("q")
        await stream.__aiter__().__anext__()
        raised: type = type(None)
        try:
            stream.close()
        except Exception as error:  # exa-py 2.25.0 + httpx raise here; see below
            raised = type(error)
        after_close()
        await stream._raw_response.aclose()
        return raised
    finally:
        await client.client.aclose()


def test_async_stream_close_ends_the_span_now_and_still_delegates(fake):
    # The vendor's close() calls httpx's sync close on an async response,
    # which raises. Tracing must not change that outcome.
    vendor_outcome = asyncio.run(_close_after_one_chunk(fake))

    with instrumented() as traced:
        outcome = asyncio.run(
            _close_after_one_chunk(fake, after_close=lambda: _assert_cancelled(traced.one()))
        )

    assert outcome is vendor_outcome


def test_async_stream_aclose_releases_the_http_response(fake):
    async def call(traced) -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            stream = await client.stream_answer("q")
            await stream.__anext__()
            await stream.aclose()
            _assert_cancelled(traced.one())
            assert stream._raw_response.is_closed
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call(traced))


def test_sync_stream_close_ends_the_span_even_if_the_vendor_close_fails(fake, monkeypatch):
    def broken_close(_self):
        raise OSError("socket already gone")

    monkeypatch.setattr(StreamSearchResponse, "close", broken_close)
    with instrumented() as traced:
        stream = Exa(api_key=EXA_KEY, base_url=fake.origin).stream_search("q")
        next(iter(stream))
        with pytest.raises(OSError, match="socket already gone"):
            stream.close()
        _assert_cancelled(traced.one())


@pytest.fixture()
def double_end_warnings(caplog):
    """The SDK logs, rather than raises, when an ended span is touched again."""
    caplog.set_level(logging.WARNING, logger="opentelemetry.sdk.trace")
    return lambda: [r.getMessage() for r in caplog.records if "ended span" in r.getMessage()]


def _assert_complete(span) -> None:
    values = attrs(span)
    assert span.status.status_code is StatusCode.OK
    assert values["fi.retrieval.document_count"] == STREAM_CITATIONS
    assert "exa.cancelled" not in values
    assert not span.events


def test_sync_streams_complete_as_one_ok_span_and_are_ended_once(fake, double_end_warnings):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        for method in (client.stream_search, client.stream_answer):
            stream = method("q")
            assert len(list(stream)) == STREAM_CHUNKS
            stream.close()  # after completion: must not re-end or flip to ERROR
            del stream
            gc.collect()

    assert [span.name for span in traced.spans()] == ["exa.search", "exa.answer"]
    for span in traced.spans():
        _assert_complete(span)
    for marker in CONTENT_MARKERS:
        assert marker not in traced.wire()
    assert double_end_warnings() == []


def test_async_streams_complete_as_one_ok_span_and_are_ended_once(fake, double_end_warnings):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            for method in (client.stream_search, client.stream_answer):
                stream = await method("q")
                chunks = [chunk async for chunk in stream]
                assert len(chunks) == STREAM_CHUNKS
                await stream.aclose()  # after completion: no second end
                del stream
                gc.collect()
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    assert [span.name for span in traced.spans()] == ["exa.search", "exa.answer"]
    for span in traced.spans():
        _assert_complete(span)
    assert double_end_warnings() == []


def test_cancelled_streams_are_ended_once(fake, double_end_warnings):
    with instrumented() as traced:
        stream = Exa(api_key=EXA_KEY, base_url=fake.origin).stream_search("q")
        next(iter(stream))
        stream.close()
        stream.close()
        del stream
        gc.collect()

    _assert_cancelled(traced.one())
    assert double_end_warnings() == []


def _assert_failed(span) -> None:
    assert span.status.status_code is StatusCode.ERROR
    assert "401" in span.status.description
    assert [event.name for event in span.events] == ["exception"]
    assert "exa.cancelled" not in attrs(span)
    assert "fi.retrieval.document_count" not in attrs(span)


def test_sync_stream_non_200_is_an_error_and_reraised(fake, double_end_warnings):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        for method in (client.stream_search, client.stream_answer):
            with pytest.raises(ValueError, match="401"):
                method(FAIL_QUERY)

    assert [span.name for span in traced.spans()] == ["exa.search", "exa.answer"]
    for span in traced.spans():
        _assert_failed(span)
    assert double_end_warnings() == []


def test_async_stream_non_200_is_an_error_and_reraised(fake, double_end_warnings):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            for method in (client.stream_search, client.stream_answer):
                with pytest.raises(ValueError, match="401"):
                    await method(FAIL_QUERY)
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    assert [span.name for span in traced.spans()] == ["exa.search", "exa.answer"]
    for span in traced.spans():
        _assert_failed(span)
    assert double_end_warnings() == []
