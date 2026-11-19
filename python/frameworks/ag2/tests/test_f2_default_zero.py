"""F2 AG2 default-zero controls, run in the AG2 package job."""

import pytest


ONE_BYTE = "data:image/png;base64,QQ=="


@pytest.fixture(autouse=True)
def _f2_clean_env(monkeypatch):
    for name in (
        "FI_HIDE_LLM_INVOCATION_PARAMETERS", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_IMAGES",
        "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_EMBEDDING_VECTORS",
        "FI_BASE64_IMAGE_MAX_LENGTH", "FI_PII_REDACTION", "FI_HIDE_ERROR_DETAILS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_f2_ag2_one_byte_path(monkeypatch):
    from traceai_ag2._processor import normalize_attributes
    from fi_instrumentation.instrumentation.config import TraceConfig
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    caller = {"gen_ai.operation.name": "chat", "gen_ai.input.messages": ONE_BYTE}
    for config in (TraceConfig(), TraceConfig(base64_image_max_length=0)):
        result = normalize_attributes(caller, config=config)
        assert result["gen_ai.input.messages"] == "[image-redacted]"
        assert result["input.value"] == "[image-redacted]"
    assert caller["gen_ai.input.messages"] == ONE_BYTE
