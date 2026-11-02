"""Granular message policies also cover opaque duplicate representations."""

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE, TraceConfig


@pytest.mark.parametrize(
    "flag", ["hide_input_text", "hide_input_messages", "hide_input_images"]
)
@pytest.mark.parametrize(
    "key",
    [
        SpanAttributes.INPUT_VALUE,
        SpanAttributes.RAW_INPUT,
        SpanAttributes.GEN_AI_REQUEST_PARAMETERS,
    ],
)
@pytest.mark.parametrize("suffix", ["", ".messages.0.content"])
def test_input_aggregate_is_hidden_without_evaluating_it(flag, key, suffix):
    config = TraceConfig(**{flag: True})

    def sensitive():
        pytest.fail("Hidden aggregate must not be evaluated")

    assert config.mask(key + suffix, sensitive) == REDACTED_VALUE
    assert config.mask(SpanAttributes.OUTPUT_VALUE, "answer") == "answer"
    assert config.mask(SpanAttributes.GEN_AI_REQUEST_MODEL, "model") == "model"
    assert config.mask(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, 3) == 3


@pytest.mark.parametrize("flag", ["hide_output_text", "hide_output_messages"])
@pytest.mark.parametrize(
    "key", [SpanAttributes.OUTPUT_VALUE, SpanAttributes.RAW_OUTPUT]
)
@pytest.mark.parametrize("suffix", ["", ".choices.0.message.content"])
def test_output_aggregate_is_hidden_without_evaluating_it(flag, key, suffix):
    config = TraceConfig(**{flag: True})

    def sensitive():
        pytest.fail("Hidden aggregate must not be evaluated")

    assert config.mask(key + suffix, sensitive) == REDACTED_VALUE
    assert config.mask(SpanAttributes.INPUT_VALUE, "prompt") == "prompt"
    assert config.mask(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, 3) == 3


def test_hide_input_messages_covers_separate_image_copy():
    assert (
        TraceConfig(hide_input_messages=True).mask(
            SpanAttributes.INPUT_IMAGES, "secret-image"
        )
        is None
    )


def test_default_capture_and_similar_metadata_keys_stay_visible():
    for key in (
        SpanAttributes.INPUT_VALUE,
        SpanAttributes.OUTPUT_VALUE,
        SpanAttributes.RAW_INPUT,
        SpanAttributes.RAW_OUTPUT,
    ):
        assert TraceConfig().mask(key, "content") == "content"
        assert (
            TraceConfig(hide_input_text=True, hide_output_text=True).mask(
                key + "_size", 3
            )
            == 3
        )
