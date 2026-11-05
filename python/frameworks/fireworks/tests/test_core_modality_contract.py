"""The deprecated route uses core's typed policy, not a pre-sanitized marker."""
import copy
import json

import pytest
from fi_instrumentation import TraceConfig
from traceai_fireworks._wrappers import _get_input_attributes

from .test_privacy_and_raw_streams import configured_tracing
from .test_privacy_regressions import IMAGE, MESSAGES, MODEL, PROMPT, call_client, exported


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["normal", "stream", "raw", "raw_stream"])
def test_text_hiding_retains_typed_image_reference(asynchronous, mode):
    with configured_tracing(hide_input_text=True) as exporter:
        requests = []
        call_client(asynchronous, mode, requests)
        attrs, everything = exported(exporter)
        assert PROMPT not in everything
        assert IMAGE in attrs["input.value"]
        content = json.loads(attrs["input.value"])["messages"][0]["content"]
        assert content[0]["text"] == "__REDACTED__"
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


def test_core_not_instrumentor_applies_image_mask():
    module = pytest.importorskip("fi_instrumentation.instrumentation._chat_input")
    original = copy.deepcopy(MESSAGES)
    attrs = _get_input_attributes(MODEL, original, config=TraceConfig(hide_input_images=True))
    assert type(attrs["input.value"]) is module.ChatInput
    # Reapply a different policy to the same ORIGINAL typed data. This cannot
    # work with a package-sanitized string or a boolean assertion of trust.
    visible = TraceConfig().mask("input.value", attrs["input.value"])
    assert IMAGE in visible and PROMPT in visible
    hidden = TraceConfig(hide_input_text=True).mask("input.value", attrs["input.value"])
    assert IMAGE in hidden and PROMPT not in hidden
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
