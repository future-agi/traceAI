"""voyageai.Client.embed: one EMBEDDING span per call, through the real SDK."""

from __future__ import annotations

import warnings
from typing import Any, Dict

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

import voyageai  # noqa: E402
from opentelemetry.trace import SpanKind  # noqa: E402

from _support import (  # noqa: E402
    CONTENT_MARKERS,
    DIMENSION,
    EMBED_MODEL,
    TEXTS,
    TOKENS_PER_TEXT,
    VECTOR_BASE,
    VECTOR_MARKER,
    VOYAGE_KEY,
    FakeVoyage,
    attrs,
    client,
    instrumented,
    status_code,
)


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def _no_vector_values(values: Dict[str, Any]) -> None:
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            assert len(value) != DIMENSION, key
            assert not any(isinstance(item, float) for item in value), key


def test_embed_emits_one_embedding_span_with_counts_dimension_and_tokens(fake):
    with instrumented() as traced:
        result = client(fake).embed(TEXTS, model=EMBED_MODEL, input_type="document")

    # The caller gets the real SDK result, unchanged.
    assert len(result.embeddings) == 2
    assert result.embeddings[0][0] == pytest.approx(VECTOR_BASE)
    assert result.total_tokens == TOKENS_PER_TEXT * 2
    # The real client made the HTTP call with its own key.
    assert fake.paths() == ["/v1/embeddings"]
    assert fake.calls[0][2] is True

    span = traced.one()
    assert span.name == "voyage.embed"
    assert span.kind == SpanKind.INTERNAL
    values = attrs(span)
    assert values["gen_ai.span.kind"] == "EMBEDDING"
    assert values["gen_ai.provider.name"] == "voyage"
    assert values["gen_ai.operation.name"] == "embeddings"
    assert values["gen_ai.request.model"] == EMBED_MODEL
    assert values["embedding.model_name"] == EMBED_MODEL
    assert values["voyage.input_type"] == "document"
    assert values["voyage.embedding.count"] == 2
    assert values["voyage.embedding.dimension"] == DIMENSION
    assert values["gen_ai.embeddings.dimension.count"] == DIMENSION
    # Voyage reports one total; for an embedding call every token is input.
    assert values["gen_ai.usage.input_tokens"] == TOKENS_PER_TEXT * 2
    assert values["gen_ai.usage.total_tokens"] == TOKENS_PER_TEXT * 2
    assert "gen_ai.usage.output_tokens" not in values
    assert values["server.address"] == "127.0.0.1"
    # The wrapper sets OK itself (no register() processor in this provider).
    assert status_code(span) == "OK"


def test_embed_span_carries_no_vectors_texts_or_key_by_default(fake):
    with instrumented() as traced:
        client(fake).embed(TEXTS, model=EMBED_MODEL, input_type="query")

    values = attrs(traced.one())
    _no_vector_values(values)
    for key in ("input.value", "output.value", "embedding.embeddings"):
        assert not any(name.startswith(key) for name in values), key
    wire = traced.wire()
    assert VECTOR_MARKER not in wire
    assert VOYAGE_KEY not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker


def test_embed_reads_positional_arguments(fake):
    with instrumented() as traced:
        client(fake).embed(TEXTS, EMBED_MODEL, "query")

    values = attrs(traced.one())
    assert values["embedding.model_name"] == EMBED_MODEL
    assert values["voyage.input_type"] == "query"
    assert values["voyage.embedding.count"] == 2


def test_embed_records_requested_dimension_and_dtype(fake):
    with instrumented() as traced:
        result = client(fake).embed(TEXTS, model=EMBED_MODEL, output_dimension=32)

    assert len(result.embeddings[0]) == 32
    values = attrs(traced.one())
    assert values["voyage.output_dimension"] == 32
    assert values["voyage.embedding.dimension"] == 32
    assert "voyage.output_dtype" not in values
    assert "voyage.input_type" not in values


def test_packed_binary_embeddings_report_the_unpacked_dimension(fake):
    with instrumented() as traced:
        result = client(fake).embed(
            TEXTS, model=EMBED_MODEL, output_dtype="binary", output_dimension=64
        )

    # binary/ubinary pack eight dimensions per returned integer.
    assert len(result.embeddings[0]) == 8
    values = attrs(traced.one())
    assert values["voyage.output_dtype"] == "binary"
    assert values["voyage.embedding.dimension"] == 64
    _no_vector_values(values)


def test_a_bare_string_counts_as_one_text(fake):
    with instrumented() as traced:
        result = client(fake).embed("one text only", model=EMBED_MODEL)

    assert len(result.embeddings) == 1
    assert attrs(traced.one())["voyage.embedding.count"] == 1


def test_the_sdk_default_model_is_recorded_when_none_is_passed(fake):
    with instrumented() as traced:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            client(fake).embed(TEXTS)

    assert fake.bodies()[0]["model"] == voyageai.VOYAGE_EMBED_DEFAULT_MODEL
    values = attrs(traced.one())
    assert values["embedding.model_name"] == voyageai.VOYAGE_EMBED_DEFAULT_MODEL
    assert values["gen_ai.request.model"] == voyageai.VOYAGE_EMBED_DEFAULT_MODEL


def test_one_span_covers_the_clients_own_retries(fake):
    with instrumented() as traced:
        result = client(fake, max_retries=2).embed(TEXTS, model="rate-limit-once")

    assert len(result.embeddings) == 2
    # The SDK retried the 429 itself; the call is still one span.
    assert fake.paths() == ["/v1/embeddings", "/v1/embeddings"]
    span = traced.one()
    assert status_code(span) == "OK"
    assert attrs(span)["gen_ai.usage.total_tokens"] == TOKENS_PER_TEXT * 2


def test_unwrapped_methods_produce_no_span(fake):
    with instrumented() as traced:
        result = client(fake).multimodal_embed(inputs=[["hello"]], model="voyage-multimodal-3")
        assert len(result.embeddings) == 1
        # Neither multimodal_embed nor contextualized_embed is wrapped (PRD R-07).
        for cls in (voyageai.Client, voyageai.AsyncClient):
            for name in ("multimodal_embed", "contextualized_embed"):
                assert not hasattr(cls.__dict__[name], "__wrapped__"), (cls, name)

    assert fake.paths() == ["/v1/multimodalembeddings"]
    assert traced.spans() == []


def test_a_local_model_is_traced_without_a_server_address(fake, monkeypatch):
    helpers = pytest.importorskip(
        "voyageai.local.helpers", reason="voyageai < 0.5 has no local embedding models"
    )
    import voyageai.client as client_module
    from voyageai.object import EmbeddingsObject

    model = "voyage-4-nano"
    assert helpers.is_local_model(model)

    def embed_local(texts, model, **kwargs):
        # Stands in for the sentence-transformers backend (torch is not installed).
        result = EmbeddingsObject()
        result.embeddings = [[VECTOR_BASE] * DIMENSION for _ in texts]
        result.total_tokens = 5
        return result

    monkeypatch.setattr(client_module, "embed_local", embed_local)
    with instrumented() as traced:
        # The client has a key and a base URL, but a local model sends no request.
        result = client(fake).embed(TEXTS, model=model)

    assert len(result.embeddings) == 2
    assert fake.paths() == []
    values = attrs(traced.one())
    assert values["embedding.model_name"] == model
    assert values["voyage.embedding.count"] == 2
    assert values["gen_ai.usage.total_tokens"] == 5
    assert "server.address" not in values
