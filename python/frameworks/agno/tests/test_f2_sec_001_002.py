"""Pin the F2 SEC-001 cap ceiling and SEC-002 invalid-env fallback."""

from dataclasses import fields

import pytest
from fi_instrumentation.instrumentation import TraceConfig as FITraceConfig
from openinference.instrumentation import TraceConfig as OITraceConfig
from traceai_agno._wrappers._privacy import _PrivacyConfig, privacy_configs


ONE_BYTE = "data:image/png;base64,QQ=="
INVALID_OI_CAPS = ("", " ", "\t\n", "abc", "NaN", "1.5", "1e3", "0x20", "+", "-")


@pytest.fixture(autouse=True)
def _clean_privacy_env(monkeypatch):
    # Isolate both namespaces; inherited hide flags must not mask a cap bug.
    for prefix, config_type in (("FI_", FITraceConfig), ("OPENINFERENCE_", OITraceConfig)):
        for field in fields(config_type):
            monkeypatch.delenv(prefix + field.name.upper(), raising=False)


@pytest.fixture
def oi_default():
    # Read the invariant default, not a new config under a test's OI env cap.
    cap_field = next(
        field for field in fields(OITraceConfig)
        if field.name == "base64_image_max_length"
    )
    default = cap_field.metadata.get("default_value", cap_field.default)
    assert isinstance(default, int) and not isinstance(default, bool)
    assert default > len(ONE_BYTE) + 1
    # The autouse fixture has cleared both namespaces at this point.
    assert OITraceConfig().base64_image_max_length == default
    return default


def _fi_cap(monkeypatch, default, case):
    cap = {
        "unset": None,
        "zero": 0,
        "below": default - 1,
        "equal": default,
        "above-by-one": default + 1,
        "above-double": default * 2,
        "above-100x": default * 100,
    }[case]
    if cap is None:
        monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    else:
        monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", str(cap))
    return cap


def _supplied_config(mode, default):
    if mode == "explicit-default":
        return OITraceConfig(base64_image_max_length=default)
    if mode == "hide-inputs-only":
        return OITraceConfig(hide_inputs=True)
    assert mode == "default"
    return OITraceConfig()


def _assert_resolution(supplied, expected):
    before = {field.name: getattr(supplied, field.name) for field in fields(supplied)}
    oi, fi = privacy_configs(supplied)
    assert oi.base64_image_max_length == fi.base64_image_max_length == expected
    assert oi is not supplied
    assert {field.name: getattr(supplied, field.name) for field in fields(supplied)} == before
    assert oi.hide_inputs is fi.hide_inputs is supplied.hide_inputs
    if expected == 0:
        # A cap of zero must protect both the FI and composed OI write paths.
        assert fi.redact_value(ONE_BYTE) == "[image-redacted]"
        assert _PrivacyConfig(oi, fi).mask("arbitrary", ONE_BYTE) == "[image-redacted]"
    return oi, fi


@pytest.mark.parametrize(
    "fi_case",
    ("above-by-one", "above-double", "above-100x", "equal", "below", "zero", "unset"),
)
@pytest.mark.parametrize("mode", ("default", "explicit-default", "hide-inputs-only"))
def test_sec_001_supplied_default_cap_never_exceeds_oi_default(
    monkeypatch, oi_default, fi_case, mode
):
    fi_cap = _fi_cap(monkeypatch, oi_default, fi_case)
    supplied = _supplied_config(mode, oi_default)
    assert supplied.base64_image_max_length == oi_default
    expected = min(oi_default, 0 if fi_cap is None else fi_cap)
    _assert_resolution(supplied, expected)


@pytest.mark.parametrize("oi_cap", INVALID_OI_CAPS)
@pytest.mark.parametrize("fi_case", ("unset", "zero", "below", "above-double"))
@pytest.mark.parametrize("mode", ("default", "hide-inputs-only"))
def test_sec_002_invalid_oi_env_is_not_an_explicit_cap(
    monkeypatch, oi_default, oi_cap, fi_case, mode
):
    fi_cap = _fi_cap(monkeypatch, oi_default, fi_case)
    monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", oi_cap)
    supplied = _supplied_config(mode, oi_default)
    # Empty/unparseable OI env falls back to D, not explicit caller policy.
    assert supplied.base64_image_max_length == oi_default
    expected = min(oi_default, 0 if fi_cap is None else fi_cap)
    _assert_resolution(supplied, expected)


@pytest.mark.parametrize(
    "spelling", ("zero", "default", "above", "padded-default", "plus-default", "minus-zero")
)
@pytest.mark.parametrize("fi_case", ("unset", "zero", "below", "above-double"))
@pytest.mark.parametrize("mode", ("default", "hide-inputs-only"))
def test_sec_002_valid_oi_env_remains_explicit(
    monkeypatch, oi_default, spelling, fi_case, mode
):
    fi_cap = _fi_cap(monkeypatch, oi_default, fi_case)
    oi_cap = {
        "zero": "0",
        "default": str(oi_default),
        "above": str(oi_default + 17),
        "padded-default": f" \t{oi_default}\n",
        "plus-default": f"+{oi_default}",
        "minus-zero": "-0",
    }[spelling]
    monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", oi_cap)
    supplied = _supplied_config(mode, oi_default)
    assert supplied.base64_image_max_length == int(oi_cap)
    expected = int(oi_cap) if fi_cap is None else min(int(oi_cap), fi_cap)
    _assert_resolution(supplied, expected)


@pytest.mark.parametrize("oi_cap", ("", "abc", "1.5"))
@pytest.mark.parametrize("fi_case", ("unset", "below"))
def test_sec_002_invalid_oi_env_preserves_nondefault_caller_cap(
    monkeypatch, oi_default, oi_cap, fi_case
):
    fi_cap = _fi_cap(monkeypatch, oi_default, fi_case)
    monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", oi_cap)
    caller_cap = oi_default + 7
    supplied = OITraceConfig(base64_image_max_length=caller_cap, hide_inputs=True)
    expected = caller_cap if fi_cap is None else min(caller_cap, fi_cap)
    _assert_resolution(supplied, expected)


@pytest.mark.parametrize("oi_cap", ("", "abc", "64000", "0"))
@pytest.mark.parametrize("fi_case", ("unset", "below"))
def test_sec_002_bridge_created_config_still_uses_only_fi_cap(
    monkeypatch, oi_default, oi_cap, fi_case
):
    fi_cap = _fi_cap(monkeypatch, oi_default, fi_case)
    monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", oi_cap)
    oi, fi = privacy_configs(None)
    expected = 0 if fi_cap is None else fi_cap
    assert oi.base64_image_max_length == fi.base64_image_max_length == expected
    if expected == 0:
        assert fi.redact_value(ONE_BYTE) == "[image-redacted]"
        assert _PrivacyConfig(oi, fi).mask("arbitrary", ONE_BYTE) == "[image-redacted]"


@pytest.mark.parametrize("fi_case", ("unset", "zero", "below", "above-double"))
@pytest.mark.parametrize(
    "oi_case",
    ("unset", "valid-zero", "valid-default", "valid-above-default", "negative", "non-integer"),
)
@pytest.mark.parametrize(
    "mode",
    (
        "bridge-created",
        "default",
        "hide-inputs-only",
        "explicit-default",
        "explicit-below-default",
        "explicit-above-default",
        "explicit-zero",
    ),
)
def test_sec_002_environment_cap_cross_product(
    monkeypatch, oi_default, fi_case, oi_case, mode
):
    """Every FI/OI/caller cell checks caps, retained keys and image policy."""
    fi_cap = _fi_cap(monkeypatch, oi_default, fi_case)
    fi_default = 0 if fi_cap is None else fi_cap
    oi_env, implicit_cap, valid_oi_env = {
        "unset": (None, oi_default, False),
        "valid-zero": ("0", 0, True),
        "valid-default": (str(oi_default), oi_default, True),
        "valid-above-default": (str(oi_default + 17), oi_default + 17, True),
        "negative": ("-1", -1, False),
        "non-integer": ("not-an-integer", oi_default, False),
    }[oi_case]
    if oi_env is None:
        monkeypatch.delenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", raising=False)
    else:
        monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", oi_env)

    if mode == "bridge-created":
        oi, fi = privacy_configs(None)
        expected = fi_default
        assert oi.base64_image_max_length == fi.base64_image_max_length == expected
        assert oi.hide_inputs is fi.hide_inputs is False
    else:
        if mode in ("default", "hide-inputs-only"):
            supplied = _supplied_config(mode, oi_default)
            caller_cap = implicit_cap
        else:
            caller_cap = {
                "explicit-default": oi_default,
                "explicit-below-default": oi_default - 1,
                "explicit-above-default": oi_default + 7,
                "explicit-zero": 0,
            }[mode]
            supplied = OITraceConfig(base64_image_max_length=caller_cap)
        # Establish the dependency parser's behavior, not merely the bridge's.
        # In the negative/implicit cells OI accepts -1; FI must normalize it to 0.
        assert supplied.base64_image_max_length == caller_cap
        expected = max(caller_cap, 0)
        if not valid_oi_env and caller_cap == oi_default:
            expected = min(expected, fi_default)
        if fi_cap is not None:
            expected = min(expected, fi_cap)
        oi, fi = _assert_resolution(supplied, expected)

    composed = _PrivacyConfig(oi, fi)
    # A retained arbitrary key avoids a content-hide rule hiding a cap bug.
    # Check short-image retention at positive caps as well as zero-cap redaction.
    short_expected = "[image-redacted]" if expected == 0 else ONE_BYTE
    oversized = "data:image/png;base64," + "QUFB" * (expected // 4 + 1)
    oversized_expected = "[image-redacted]" if expected == 0 else "__REDACTED__"
    for original, masked in (
        ("synthetic-safe-text", "synthetic-safe-text"),
        (ONE_BYTE, short_expected),
        (oversized, oversized_expected),
    ):
        assert fi.redact_value(original) == masked
        assert fi.mask("arbitrary", original) == masked
        assert composed.mask("arbitrary", original) == masked
