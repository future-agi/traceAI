"""Shared-harness contract test for traceAI-voyage (TH-8321, TH-8339 Receiver).

The real ``voyageai`` client makes real HTTP calls to a loopback fake of the
Voyage API (``Client(base_url=...)``, a choice the test makes, never the
instrumentor). Spans leave through the real ``fi_instrumentation.register()``
OTLP exporter into ``harness.Receiver``. Nothing calls Voyage; every key is a
placeholder.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

import voyageai  # noqa: E402
from harness import Receiver, _flatten_attributes  # noqa: E402

from _support import (  # noqa: E402
    CONTENT_MARKERS,
    DIMENSION,
    DOCUMENTS,
    EMBED_MODEL,
    QUERY,
    RERANK_MODEL,
    SCORE_MARKERS,
    TEXTS,
    TOKENS_PER_TEXT,
    VECTOR_MARKER,
    VOYAGE_KEY,
    FakeVoyage,
    async_client,
    client,
    rerank_tokens,
)

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
PROJECT = "voyage-contract"


def _journey(
    receiver: Receiver, fake: FakeVoyage, monkeypatch: pytest.MonkeyPatch, **options: Any
) -> Tuple[List[dict], List[dict]]:
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_voyage import VoyageInstrumentor

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
    instrumentor = VoyageInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        voyage = client(fake)
        assert len(voyage.embed(TEXTS, model=EMBED_MODEL, input_type="document").embeddings) == 2
        assert [r.index for r in voyage.rerank(QUERY, DOCUMENTS, RERANK_MODEL, top_k=2).results] == [
            1,
            0,
        ]
        with pytest.raises(voyageai.error.AuthenticationError):
            voyage.embed(TEXTS, model="fail-401")
        asyncio.run(async_client(fake).rerank(QUERY, DOCUMENTS, model=RERANK_MODEL))
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    return receiver.spans(), receiver.requests()


def _by_name(spans: List[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for span in spans:
        grouped.setdefault(span["name"], []).append(span)
    return grouped


def test_real_client_calls_reach_the_collector_contract(monkeypatch):
    with FakeVoyage() as fake, Receiver() as receiver:
        spans, exports = _journey(receiver, fake, monkeypatch)
        paths = fake.paths()

    # The real SDK made every HTTP call (no monkeypatched client methods).
    assert paths == ["/v1/embeddings", "/v1/rerank", "/v1/embeddings", "/v1/rerank"]

    # Collector contract on every export.
    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert "authorization" not in export["headers"]
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"

    by_name = _by_name(spans)
    assert sorted((name, len(group)) for name, group in by_name.items()) == [
        ("voyage.embed", 2),
        ("voyage.rerank", 2),
    ]

    ok_embed, failed_embed = by_name["voyage.embed"]
    embed = _flatten_attributes(ok_embed["attributes"])
    assert embed["gen_ai.span.kind"] == "EMBEDDING"
    assert embed["gen_ai.provider.name"] == "voyage"
    assert embed["gen_ai.request.model"] == EMBED_MODEL
    assert embed["embedding.model_name"] == EMBED_MODEL
    assert int(embed["voyage.embedding.count"]) == 2
    assert int(embed["voyage.embedding.dimension"]) == DIMENSION
    # Promoted keys the collector reads (fi-collector DeriveHotKeys aliases).
    assert int(embed["gen_ai.usage.input_tokens"]) == TOKENS_PER_TEXT * 2
    assert int(embed["gen_ai.usage.total_tokens"]) == TOKENS_PER_TEXT * 2
    # register() also promotes UNSET to OK on export; the wrapper's own OK is
    # pinned by the in-memory tests (test_embed, test_rerank, test_async).
    assert ok_embed["status"]["code"] == "STATUS_CODE_OK"

    for rerank_span in by_name["voyage.rerank"]:
        rerank = _flatten_attributes(rerank_span["attributes"])
        assert rerank["gen_ai.span.kind"] == "RERANKER"
        assert rerank["reranker.model_name"] == RERANK_MODEL
        assert int(rerank["voyage.rerank.document_count"]) == 3
        assert int(rerank["gen_ai.usage.total_tokens"]) == rerank_tokens(3)
        assert rerank_span["status"]["code"] == "STATUS_CODE_OK"
    assert int(_flatten_attributes(by_name["voyage.rerank"][0]["attributes"])["reranker.top_k"]) == 2

    failed = _flatten_attributes(failed_embed["attributes"])
    assert failed_embed["status"]["code"] == "STATUS_CODE_ERROR"
    assert any(event["name"] == "exception" for event in failed_embed.get("events", []))
    assert "gen_ai.usage.total_tokens" not in failed


def test_no_key_vectors_or_content_are_exported_by_default(monkeypatch):
    with FakeVoyage() as fake, Receiver() as receiver:
        spans, exports = _journey(receiver, fake, monkeypatch)

    wire = json.dumps(spans) + json.dumps(exports)
    for secret in (VOYAGE_KEY, VECTOR_MARKER) + CONTENT_MARKERS + SCORE_MARKERS:
        assert secret not in wire, secret


def test_capture_content_control_run_puts_the_markers_on_the_wire(monkeypatch):
    # Pairs the absence check above with a run that does export content, so
    # the absence cannot come from dropped spans or markers that never flowed.
    with FakeVoyage() as fake, Receiver() as receiver:
        spans, _exports = _journey(receiver, fake, monkeypatch, capture_content=True)

    wire = json.dumps(spans)
    for marker in CONTENT_MARKERS + (SCORE_MARKERS[1],):
        assert marker in wire, marker
    # Vectors and the key stay off the wire even with capture on.
    assert VECTOR_MARKER not in wire
    assert VOYAGE_KEY not in wire
