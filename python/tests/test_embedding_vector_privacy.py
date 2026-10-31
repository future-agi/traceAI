import pytest

from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import TraceConfig


VECTOR_KEYS = (
    SpanAttributes.GEN_AI_EMBEDDINGS_VECTORS,
    f"{SpanAttributes.GEN_AI_EMBEDDINGS_VECTORS}.0",
    f"{SpanAttributes.GEN_AI_EMBEDDINGS_VECTORS}.0.values",
    "embedding.embeddings.0.embedding.vector",
)


@pytest.mark.parametrize("key", VECTOR_KEYS)
@pytest.mark.parametrize("from_environment", (False, True))
def test_hide_embedding_vectors_covers_public_and_legacy_keys(
    monkeypatch, key, from_environment
):
    monkeypatch.setenv("FI_HIDE_EMBEDDING_VECTORS", "true")
    config = TraceConfig() if from_environment else TraceConfig(hide_embedding_vectors=True)
    assert config.mask(key, [0.012, 0.987]) is None


@pytest.mark.parametrize("key", VECTOR_KEYS)
def test_embedding_vectors_remain_visible_when_explicitly_enabled(monkeypatch, key):
    monkeypatch.setenv("FI_HIDE_EMBEDDING_VECTORS", "true")
    vector = [0.012, 0.987]
    assert TraceConfig(hide_embedding_vectors=False).mask(key, vector) == vector


@pytest.mark.parametrize(
    "key,value",
    (
        ("gen_ai.embeddings.dimension", 2),
        ("gen_ai.embeddings.vectors_count", 1),
        ("gen_ai.request.model", "embedding-model"),
        ("embedding.embeddings.0.embedding.model_name", "embedding-model"),
    ),
)
def test_hiding_embedding_vectors_preserves_metadata(key, value):
    assert TraceConfig(hide_embedding_vectors=True).mask(key, value) == value


def test_hidden_vector_does_not_evaluate_lazy_value():
    def vector():
        raise AssertionError("Hidden vector must not be evaluated")

    assert TraceConfig(hide_embedding_vectors=True).mask(
        SpanAttributes.GEN_AI_EMBEDDINGS_VECTORS, vector
    ) is None
