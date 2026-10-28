"""voyageai.Client.rerank: one RERANKER span per call, through the real SDK."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

from _support import (  # noqa: E402
    DOCUMENTS,
    QUERY,
    RERANK_MODEL,
    SCORES,
    VOYAGE_KEY,
    FakeVoyage,
    attrs,
    client,
    instrumented,
    rerank_tokens,
    status_code,
)


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def test_rerank_emits_one_reranker_span_with_counts_top_k_and_tokens(fake):
    with instrumented() as traced:
        result = client(fake).rerank(QUERY, DOCUMENTS, model=RERANK_MODEL, top_k=2)

    # The client's order is the result order; nothing is re-sorted.
    assert [r.index for r in result.results] == [1, 0]
    assert fake.paths() == ["/v1/rerank"]

    span = traced.one()
    assert span.name == "voyage.rerank"
    values = attrs(span)
    assert values["gen_ai.span.kind"] == "RERANKER"
    assert values["gen_ai.provider.name"] == "voyage"
    assert values["gen_ai.operation.name"] == "rerank"
    assert values["gen_ai.request.model"] == RERANK_MODEL
    assert values["reranker.model_name"] == RERANK_MODEL
    assert values["reranker.top_k"] == 2
    assert values["voyage.rerank.document_count"] == 3
    assert values["voyage.rerank.result_count"] == 2
    assert values["gen_ai.usage.input_tokens"] == rerank_tokens(3)
    assert values["gen_ai.usage.total_tokens"] == rerank_tokens(3)
    assert values["server.address"] == "127.0.0.1"
    assert status_code(span) == "OK"


def test_rerank_span_carries_the_query_and_scores_but_no_documents_or_key_by_default(fake):
    with instrumented() as traced:
        client(fake).rerank(QUERY, DOCUMENTS, model=RERANK_MODEL, top_k=2)

    values = attrs(traced.one())
    # PRD J2 / AC-03: query and scores are on by default; documents are opt-in.
    assert values["reranker.query"] == QUERY
    assert values["input.value"] == QUERY
    assert json.loads(values["output.value"]) == [
        {"index": 1, "relevance_score": SCORES[1]},
        {"index": 0, "relevance_score": SCORES[0]},
    ]
    for key in ("reranker.input_documents", "reranker.output_documents"):
        assert not any(name.startswith(key) for name in values), key
    wire = traced.wire()
    assert VOYAGE_KEY not in wire
    for marker in DOCUMENTS:
        assert marker not in wire, marker


def test_rerank_reads_positional_arguments_and_omits_an_unset_top_k(fake):
    with instrumented() as traced:
        result = client(fake).rerank(QUERY, DOCUMENTS, RERANK_MODEL)

    assert len(result.results) == 3
    values = attrs(traced.one())
    assert values["reranker.model_name"] == RERANK_MODEL
    assert values["voyage.rerank.document_count"] == 3
    assert values["voyage.rerank.result_count"] == 3
    assert "reranker.top_k" not in values
