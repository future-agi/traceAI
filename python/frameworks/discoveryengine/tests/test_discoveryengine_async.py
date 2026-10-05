"""J6, AC-07: the async clients produce the same spans as the sync clients."""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as core_exceptions
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    FAIL_DENIED,
    PAGES,
    SEARCH_RESULTS,
    SECOND_PAGE_RESULTS,
    SLOW,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_answer_client,
    async_search_client,
    attrs,
    event,
    instrumented,
    search_client,
    search_request,
)


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


def _sync_journey(fake):
    search = search_client(fake)
    search.search(request=search_request("parity"))
    search.search_lite(request=search_request("parity"))
    answer_client(fake).answer_query(request=answer_request("parity"))


async def _async_journey(fake):
    search = async_search_client(fake)
    answer = async_answer_client(fake)
    try:
        await search.search(request=search_request("parity"))
        await search.search_lite(request=search_request("parity"))
        await answer.answer_query(request=answer_request("parity"))
    finally:
        await search.transport.close()
        await answer.transport.close()


@pytest.mark.parametrize("capture_query", [False, True])
def test_async_spans_match_sync_spans(fake, capture_query):
    with instrumented(capture_query=capture_query) as traced:
        _sync_journey(fake)
        asyncio.run(_async_journey(fake))

    spans = traced.spans()
    assert [span.name for span in spans] == [
        "discoveryengine.search",
        "discoveryengine.search_lite",
        "discoveryengine.answer_query",
    ] * 2
    for sync_span, async_span in zip(spans[:3], spans[3:]):
        assert attrs(async_span) == attrs(sync_span), sync_span.name
        assert async_span.status.status_code is StatusCode.OK
    assert fake.methods() == ["Search", "SearchLite", "AnswerQuery"] * 2


def test_async_pager_iteration_keeps_one_span(fake):
    async def call():
        client = async_search_client(fake)
        try:
            pager = await client.search(request=search_request(PAGES))
            return [result async for result in pager]
        finally:
            await client.transport.close()

    with instrumented() as traced:
        results = asyncio.run(call())

    assert len(results) == SEARCH_RESULTS + SECOND_PAGE_RESULTS
    assert fake.methods() == ["Search", "Search"]
    assert attrs(traced.one())["discoveryengine.result_count"] == SEARCH_RESULTS


def test_async_error_matches_sync_error(fake):
    async def call():
        client = async_search_client(fake)
        try:
            await client.search(request=search_request(FAIL_DENIED))
        finally:
            await client.transport.close()

    with instrumented() as traced:
        with pytest.raises(core_exceptions.PermissionDenied):
            search_client(fake).search(request=search_request(FAIL_DENIED))
        with pytest.raises(core_exceptions.PermissionDenied):
            asyncio.run(call())

    sync_span, async_span = traced.spans()
    assert attrs(async_span) == attrs(sync_span)
    assert async_span.status.status_code is StatusCode.ERROR
    assert async_span.status.description == sync_span.status.description
    assert event(async_span, "exception")["exception.message"] == event(sync_span, "exception")["exception.message"]


def test_cancelling_an_async_call_ends_the_span_as_cancelled(fake):
    async def call():
        client = async_answer_client(fake)
        try:
            task = asyncio.ensure_future(client.answer_query(request=answer_request(SLOW)))
            while not fake.received.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.transport.close()

    with instrumented() as traced:
        asyncio.run(call())

    span = traced.one()
    assert span.name == "discoveryengine.answer_query"
    assert attrs(span)["discoveryengine.cancelled"] is True
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert span.events == ()
    assert "discoveryengine.result_count" not in attrs(span)


def test_concurrent_async_calls_get_one_span_each(fake):
    async def call():
        client = async_search_client(fake)
        try:
            await asyncio.gather(*(client.search(request=search_request("q{0}".format(i))) for i in range(5)))
        finally:
            await client.transport.close()

    with instrumented() as traced:
        asyncio.run(call())

    assert traced.names() == ["discoveryengine.search"] * 5
    assert len({span.context.span_id for span in traced.spans()}) == 5
    assert all(span.parent is None for span in traced.spans())
