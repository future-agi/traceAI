"""Stream lifecycle from the real SDK against the loopback fake's SSE routes."""

from __future__ import annotations

import asyncio
import gc

import pytest
from opentelemetry.trace import StatusCode

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import SLOW_QUERY, EXA_KEY, FakeExa, attrs, instrumented  # noqa: E402
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
