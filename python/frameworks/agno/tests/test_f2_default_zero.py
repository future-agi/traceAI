"""F2 Agno bridge controls, run in the Agno package job."""

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


@pytest.mark.parametrize("maximum", [0, "0", -1, float("nan"), "abc", True, 1.5])
def test_f2_agno_bridge_one_byte_and_invalid(monkeypatch, maximum):
    from openinference.instrumentation import TraceConfig as OITraceConfig
    from traceai_agno._wrappers._privacy import privacy_configs, _PrivacyConfig
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    supplied = OITraceConfig(base64_image_max_length=0)
    # Exercise the bridge's actual read of even an invalid external config;
    # avoid depending on which OI versions reject it during their constructor.
    object.__setattr__(supplied, "base64_image_max_length", maximum)
    oi, fi = privacy_configs(supplied)
    assert oi.base64_image_max_length == fi.base64_image_max_length == 0
    assert fi.redact_value(ONE_BYTE) == "[image-redacted]"
    assert _PrivacyConfig(oi, fi).mask("arbitrary", ONE_BYTE) == "[image-redacted]"


def test_f2_agno_default_env_and_explicit_positive(monkeypatch):
    from openinference.instrumentation import TraceConfig as OITraceConfig
    from traceai_agno._wrappers._privacy import privacy_configs
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    oi, fi = privacy_configs(None)
    assert oi.base64_image_max_length == fi.base64_image_max_length == 0
    assert fi.redact_value(ONE_BYTE) == "[image-redacted]"
    supplied = OITraceConfig(base64_image_max_length=64000)
    for env, expected in ((None, 64000), ("0", 0), ("abc", 0), ("-1", 0), ("NaN", 0), ("16000", 16000), ("96000", 64000)):
        if env is None:
            monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
        else:
            monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", env)
        oi, fi = privacy_configs(supplied)
        assert oi.base64_image_max_length == fi.base64_image_max_length == expected
        assert fi.redact_value(ONE_BYTE) == ("[image-redacted]" if expected == 0 else ONE_BYTE)
        assert supplied.base64_image_max_length == 64000


def test_f2_agno_supplied_config_without_cap_resolves_zero(monkeypatch):
    # Cap-0 completeness: a config passed only to
    # set another option carries OI's default cap and must not bypass cap 0.
    from openinference.instrumentation import TraceConfig as OITraceConfig
    from traceai_agno._wrappers._privacy import privacy_configs, _PrivacyConfig
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    monkeypatch.delenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", raising=False)
    supplied = OITraceConfig(hide_inputs=True)
    oi, fi = privacy_configs(supplied)
    assert oi.base64_image_max_length == fi.base64_image_max_length == 0
    assert fi.redact_value(ONE_BYTE) == "[image-redacted]"
    assert _PrivacyConfig(oi, fi).mask("arbitrary", ONE_BYTE) == "[image-redacted]"
    assert oi.hide_inputs is True
    monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", "16000")
    oi, fi = privacy_configs(OITraceConfig(hide_inputs=True))
    assert oi.base64_image_max_length == fi.base64_image_max_length == 16000


def test_f2_agno_openinference_env_cap_is_explicit(monkeypatch):
    from openinference.instrumentation import TraceConfig as OITraceConfig
    from traceai_agno._wrappers._privacy import privacy_configs
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", "48000")
    oi, fi = privacy_configs(OITraceConfig())
    assert oi.base64_image_max_length == fi.base64_image_max_length == OITraceConfig().base64_image_max_length
