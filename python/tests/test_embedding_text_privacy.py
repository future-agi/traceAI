import pytest

from fi_instrumentation.instrumentation.config import REDACTED_VALUE, TraceConfig


TEXT_KEYS = (
    "embedding.embeddings.0.embedding.text",
    "embedding.embeddings.12.embedding.text",
)
INPUT_FLAGS = (
    ("hide_inputs", "FI_HIDE_INPUTS"),
    ("hide_input_messages", "FI_HIDE_INPUT_MESSAGES"),
    ("hide_input_text", "FI_HIDE_INPUT_TEXT"),
)


@pytest.fixture(autouse=True)
def _no_hide_flags_from_the_environment(monkeypatch):
    for _, env_var in INPUT_FLAGS + (("", "FI_HIDE_OUTPUTS"), ("", "FI_HIDE_EMBEDDING_VECTORS")):
        monkeypatch.delenv(env_var, raising=False)


@pytest.mark.parametrize("key", TEXT_KEYS)
@pytest.mark.parametrize("flag,env_var", INPUT_FLAGS)
@pytest.mark.parametrize("from_environment", (False, True))
def test_input_hide_flags_redact_embedding_text(monkeypatch, key, flag, env_var, from_environment):
    if from_environment:
        monkeypatch.setenv(env_var, "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**{flag: True})
    assert config.mask(key, "synthetic private document") == REDACTED_VALUE


@pytest.mark.parametrize("key", TEXT_KEYS)
def test_embedding_text_is_kept_without_input_flags(key):
    text = "synthetic document"
    assert TraceConfig().mask(key, text) == text
    # Output and vector flags are not about the embedded input text.
    assert TraceConfig(hide_outputs=True, hide_embedding_vectors=True).mask(key, text) == text


@pytest.mark.parametrize("flag,_", INPUT_FLAGS)
def test_input_flags_keep_embedding_metadata_and_vectors(flag, _):
    config = TraceConfig(**{flag: True})
    assert config.mask("embedding.model_name", "embedding-model") == "embedding-model"
    assert config.mask("embedding.embeddings.0.embedding.vector", [0.1, 0.2]) == [0.1, 0.2]
    # A key that only looks similar is not embedding text.
    assert config.mask("metadata.embedding.text", "kept") == "kept"


def test_hidden_embedding_text_does_not_evaluate_lazy_value():
    def text():
        raise AssertionError("Hidden text must not be evaluated")

    assert TraceConfig(hide_inputs=True).mask(TEXT_KEYS[0], text) == REDACTED_VALUE
