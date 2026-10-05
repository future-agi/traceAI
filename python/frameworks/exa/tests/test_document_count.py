"""fi.retrieval.document_count from the real SDK against the loopback fake.

The count is what the call returned: search/get_contents results, answer
citations, and for streams the citations accumulated over every chunk. When
the count is unknown (error, cancellation, unrecognised result) the attribute
is absent rather than 0.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import (  # noqa: E402
    ANSWER_CITATIONS,
    EXA_KEY,
    FAIL_QUERY,
    SEARCH_RESULTS,
    STREAM_CITATIONS,
    FakeExa,
    attrs,
    instrumented,
)
from exa_py import AsyncExa, Exa  # noqa: E402

COUNT = "fi.retrieval.document_count"


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def _counts(traced):
    return [(span.name, attrs(span).get(COUNT)) for span in traced.spans()]


def test_sync_counts_results_and_citations(fake):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        client.search("q")
        assert len(client.answer("q").citations) == ANSWER_CITATIONS
        client.get_contents(["https://example.com/0"])

    assert _counts(traced) == [
        ("exa.search", SEARCH_RESULTS),
        ("exa.answer", ANSWER_CITATIONS),
        ("exa.get_contents", 1),
    ]


def test_async_counts_results_and_citations(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            await client.search("q")
            await client.answer("q")
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    assert _counts(traced) == [("exa.search", SEARCH_RESULTS), ("exa.answer", ANSWER_CITATIONS)]


def test_sync_streams_count_citations_accumulated_over_chunks(fake):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        answer_citations = sum(len(chunk.citations or []) for chunk in client.stream_answer("q"))
        search_citations = sum(len(chunk.citations or []) for chunk in client.stream_search("q"))

    assert answer_citations == search_citations == STREAM_CITATIONS
    assert _counts(traced) == [
        ("exa.answer", STREAM_CITATIONS),
        ("exa.search", STREAM_CITATIONS),
    ]


def test_async_streams_count_citations_accumulated_over_chunks(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            async for _ in await client.stream_answer("q"):
                pass
            async for _ in await client.stream_search("q"):
                pass
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    assert _counts(traced) == [
        ("exa.answer", STREAM_CITATIONS),
        ("exa.search", STREAM_CITATIONS),
    ]


def test_unknown_counts_are_omitted_not_zero(fake):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        with pytest.raises(ValueError):
            client.search(FAIL_QUERY)
        stream = client.stream_answer("q")
        next(iter(stream))
        stream.close()  # cancelled after one chunk: the total is unknown

    assert _counts(traced) == [("exa.search", None), ("exa.answer", None)]


def test_unrecognised_result_has_no_count(monkeypatch):
    def search(_self, _query, **_kwargs):
        return object()

    monkeypatch.setattr(Exa, "search", search)
    with instrumented() as traced:
        Exa(api_key=EXA_KEY).search("q")

    assert COUNT not in attrs(traced.one())
