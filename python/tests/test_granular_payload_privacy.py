"""Opaque payloads cannot bypass granular content privacy."""
import pytest

from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation.config import REDACTED_VALUE


@pytest.mark.parametrize("flag,keys", [
    ("hide_input_text", ("input.value", "raw.input", "gen_ai.prompt.template.variables")),
    ("hide_input_messages", ("input.value", "raw.input")),
    ("hide_input_images", ("input.value", "raw.input")),
    ("hide_output_text", ("output.value", "raw.output")),
    ("hide_output_messages", ("output.value", "raw.output")),
])
def test_opaque_payloads_are_not_evaluated(flag, keys):
    config = TraceConfig(**{flag: True})

    def private_value():
        raise AssertionError("private lazy value must not be evaluated")

    for key in keys:
        assert config.mask(key, private_value) == REDACTED_VALUE
        if "messages" not in key:
            assert config.mask(key + ".nested", private_value) == REDACTED_VALUE
    assert config.mask("gen_ai.request.model", "visible-model") == "visible-model"
    assert config.mask("gen_ai.usage.input_tokens", 12) == 12


@pytest.mark.parametrize("direction,opposite", [("input", "output"), ("output", "input")])
def test_text_privacy_is_directional(direction, opposite):
    config = TraceConfig(**{"hide_" + direction + "_text": True})
    assert config.mask(direction + ".value", "private") == REDACTED_VALUE
    assert config.mask(opposite + ".value", "visible") == "visible"
    assert config.mask("gen_ai." + direction + ".messages.0.message.role", "user") == "user"
    assert config.mask("gen_ai." + direction + ".messages.0.message.content", "private") == REDACTED_VALUE


def test_text_flag_keeps_explicitly_typed_images():
    # F2 (RL 2310bu): the text flag never hides typed images; pinned at the previous
    # default cap. At the default cap 0 a typed image value is an over-cap payload.
    config = TraceConfig(hide_input_text=True, base64_image_max_length=32_000)
    assert config.mask("gen_ai.input.images.0", "image-reference") == "image-reference"
    assert config.mask("gen_ai.input.messages.0.message.contents.0.message_content.image.image.url", "image-reference") == "image-reference"
    default = TraceConfig(hide_input_text=True)
    # Observed on the merged head (E2B tracebacks, final-r5-targeted line 45 and ifin): a gen_ai.input.images
    # value is not an image-cap key and keeps a non-data reference; a message image URL slot is an image key
    # and at the default cap 0 its non-empty payload is replaced whole.
    assert default.mask("gen_ai.input.images.0", "image-reference") == "image-reference"
    assert default.mask("gen_ai.input.messages.0.message.contents.0.message_content.image.image.url", "image-reference") == "[image-redacted]"
