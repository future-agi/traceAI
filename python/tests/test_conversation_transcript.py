import pytest

from fi_instrumentation import TraceConfig, allows_conversation_transcript


CONTENT_FLAGS = (
    "hide_inputs",
    "hide_input_messages",
    "hide_input_text",
    "hide_outputs",
    "hide_output_messages",
    "hide_output_text",
)


@pytest.fixture(autouse=True)
def clear_content_flags(monkeypatch):
    for flag in CONTENT_FLAGS:
        monkeypatch.delenv(f"FI_{flag.upper()}", raising=False)


def test_allows_transcript_by_default():
    assert allows_conversation_transcript(None)
    assert allows_conversation_transcript(TraceConfig())


@pytest.mark.parametrize("flag", CONTENT_FLAGS)
def test_content_flag_omits_whole_transcript(flag):
    assert not allows_conversation_transcript(TraceConfig(**{flag: True}))


@pytest.mark.parametrize("flag", CONTENT_FLAGS)
def test_default_config_reads_content_flags_from_environment(monkeypatch, flag):
    config = TraceConfig()
    monkeypatch.setenv(f"FI_{flag.upper()}", "true")
    assert not allows_conversation_transcript()
    assert allows_conversation_transcript(config)


def test_non_transcript_flags_do_not_omit_text():
    config = TraceConfig(hide_error_details=True, hide_input_images=True, hide_embedding_vectors=True)
    assert allows_conversation_transcript(config)
