"""The deprecated route uses core's typed policy, not a pre-sanitized marker."""
import copy
import json

import pytest
from fi_instrumentation import TraceConfig
from traceai_fireworks._wrappers import _get_input_attributes

from .test_privacy_and_raw_streams import DEFAULT_CAP_IS_ZERO, configured_tracing
from .test_privacy_regressions import IMAGE, MESSAGES, MODEL, PROMPT, call_client, exported


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["normal", "stream", "raw", "raw_stream"])
@pytest.mark.parametrize("cap", [None, 32_000], ids=["default-cap-0", "explicit-cap-32000"])
def test_text_hiding_retains_typed_image_reference(monkeypatch, asynchronous, mode, cap):
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    config = {"hide_input_text": True}
    if cap is not None:
        config["base64_image_max_length"] = cap
    with configured_tracing(**config) as exporter:
        requests = []
        call_client(asynchronous, mode, requests)
        attrs, everything = exported(exporter)
        assert PROMPT not in everything
        if cap is None and DEFAULT_CAP_IS_ZERO:
            assert IMAGE not in everything
        else:
            assert IMAGE in attrs["input.value"]
        content = json.loads(attrs["input.value"])["messages"][0]["content"]
        assert content[0]["text"] == "__REDACTED__"
        if cap is None and DEFAULT_CAP_IS_ZERO:
            assert content[1]["image_url"] == "[image-redacted]"
        else:
            assert content[1]["image_url"]["url"] == IMAGE
    assert requests[0]["body"]["messages"] == MESSAGES


def test_granular_core_path_never_calls_unknown_serializers():
    # API is optional only at the published pre-guard core floor. When present,
    # malformed data must fail closed rather than falling back to serialization.
    pytest.importorskip("fi_instrumentation.instrumentation._chat_input")

    class Active:
        def model_dump(self):
            raise AssertionError("hidden serializer called")

        def __str__(self):
            raise AssertionError("hidden string conversion called")

    config = TraceConfig(hide_input_images=True)
    messages = [{"role": "user", "content": Active()}]
    attrs = _get_input_attributes(MODEL, messages, config=config, stop=Active(),
                                  extra_headers={"credential": "SYNTHETIC-SECRET"})
    assert config.mask("input.value", attrs["input.value"]) == "__REDACTED__"
    assert not any(key.startswith("gen_ai.input.messages") for key in attrs)


def test_core_not_instrumentor_applies_image_mask(monkeypatch):
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    module = pytest.importorskip("fi_instrumentation.instrumentation._chat_input")
    original = copy.deepcopy(MESSAGES)
    attrs = _get_input_attributes(MODEL, original, config=TraceConfig(hide_input_images=True))
    assert type(attrs["input.value"]) is module.ChatInput
    # Reapply a different policy to the same ORIGINAL typed data. This cannot
    # work with a package-sanitized string or a boolean assertion of trust.
    # F2 (RL 2310bu): retain the original assertions at the former default cap.
    visible = TraceConfig(base64_image_max_length=32_000).mask("input.value", attrs["input.value"])
    assert IMAGE in visible and PROMPT in visible
    hidden = TraceConfig(hide_input_text=True, base64_image_max_length=32_000).mask(
        "input.value", attrs["input.value"]
    )
    assert IMAGE in hidden and PROMPT not in hidden
    default_visible = TraceConfig().mask("input.value", attrs["input.value"])
    assert IMAGE not in default_visible and PROMPT in default_visible
    content = json.loads(default_visible)["messages"][0]["content"]
    assert content[1]["image_url"] == "[image-redacted]"
    default_hidden = TraceConfig(hide_input_text=True).mask("input.value", attrs["input.value"])
    assert IMAGE not in default_hidden and PROMPT not in default_hidden
    content = json.loads(default_hidden)["messages"][0]["content"]
    assert content[0]["text"] == "__REDACTED__"
    assert content[1]["image_url"] == "[image-redacted]"
    assert original == MESSAGES


def test_repository_package_collection_with_installed_core():
    # pytest also imports the repository's python package while collecting recipe
    # and framework tests. It must not demand a new API from an older installed
    # canonical fi_instrumentation package (the supported published-floor job).
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    assert (root / "python" / "__init__.py").is_file()
    result = subprocess.run([
        sys.executable, "-I", "-c",
        "import sys, fi_instrumentation; before = fi_instrumentation.__file__; "
        "sys.path.insert(0, sys.argv[1]); import python; "
        "assert fi_instrumentation.__file__ == before",
        str(root),
    ], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
