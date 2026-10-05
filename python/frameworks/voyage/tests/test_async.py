"""voyageai.AsyncClient: the same spans as the sync client (sync/async parity)."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

import voyageai  # noqa: E402

from _support import (  # noqa: E402
    CONTENT_MARKERS,
    DOCUMENTS,
    EMBED_MODEL,
    QUERY,
    RERANK_MODEL,
    SCORE_MARKERS,
    TEXTS,
    VECTOR_MARKER,
    VOYAGE_KEY,
    FakeVoyage,
    async_client,
    attrs,
    client,
    event_names,
    instrumented,
    status_code,
)


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def _run(call: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(call())


def test_async_embed_matches_the_sync_span(fake):
    with instrumented() as traced:
        client(fake).embed(TEXTS, model=EMBED_MODEL, input_type="document")
        result = _run(
            lambda: async_client(fake).embed(TEXTS, model=EMBED_MODEL, input_type="document")
        )

    assert len(result.embeddings) == 2
    sync_span, async_span = traced.spans()
    assert async_span.name == sync_span.name == "voyage.embed"
    assert attrs(async_span) == attrs(sync_span)
    assert status_code(async_span) == "OK"


def test_async_rerank_matches_the_sync_span(fake):
    with instrumented() as traced:
        client(fake).rerank(QUERY, DOCUMENTS, model=RERANK_MODEL, top_k=2)
        result = _run(
            lambda: async_client(fake).rerank(QUERY, DOCUMENTS, model=RERANK_MODEL, top_k=2)
        )

    assert [r.index for r in result.results] == [1, 0]
    sync_span, async_span = traced.spans()
    assert async_span.name == sync_span.name == "voyage.rerank"
    assert attrs(async_span) == attrs(sync_span)
    assert status_code(async_span) == "OK"


def test_async_spans_carry_no_content_by_default(fake):
    async def calls() -> None:
        voyage = async_client(fake)
        await voyage.embed(TEXTS, model=EMBED_MODEL)
        await voyage.rerank(QUERY, DOCUMENTS, model=RERANK_MODEL)

    with instrumented() as traced:
        _run(calls)

    assert [span.name for span in traced.spans()] == ["voyage.embed", "voyage.rerank"]
    wire = traced.wire()
    for marker in CONTENT_MARKERS + SCORE_MARKERS + (VECTOR_MARKER, VOYAGE_KEY):
        assert marker not in wire, marker


def test_async_errors_match_the_sync_error_span(fake):
    with instrumented() as traced:
        with pytest.raises(voyageai.error.AuthenticationError):
            client(fake).embed(TEXTS, model="fail-401")
        with pytest.raises(voyageai.error.AuthenticationError):
            _run(lambda: async_client(fake).embed(TEXTS, model="fail-401"))

    sync_span, async_span = traced.spans()
    for span in (sync_span, async_span):
        assert status_code(span) == "ERROR"
        assert span.status.description.startswith("AuthenticationError")
        assert event_names(span) == ["exception"]
    assert attrs(async_span) == attrs(sync_span)
