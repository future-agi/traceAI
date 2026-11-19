"""Raw request and response payloads are recorded only while no hide setting is on."""
import pytest

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes

RAW_KEYS = [SpanAttributes.RAW_INPUT, SpanAttributes.RAW_OUTPUT]
SETTINGS = [
    "hide_inputs",
    "hide_outputs",
    "hide_input_messages",
    "hide_output_messages",
    "hide_input_text",
    "hide_output_text",
    "hide_input_images",
    "hide_embedding_vectors",
    "hide_llm_invocation_parameters",
]
PAYLOAD = '{"sentinel": "RAW-PAYLOAD-SENTINEL"}'


def config(**flags):
    base = {name: False for name in SETTINGS}
    base.update(flags)
    return TraceConfig(**base)


@pytest.mark.parametrize("key", RAW_KEYS)
def test_raw_payload_is_recorded_with_no_hide_setting(key):
    assert config().mask(key, PAYLOAD) == PAYLOAD


@pytest.mark.parametrize("key", RAW_KEYS)
@pytest.mark.parametrize("setting", SETTINGS)
def test_raw_payload_is_not_recorded_under_any_hide_setting(setting, key):
    assert config(**{setting: True}).mask(key, PAYLOAD) is None


@pytest.mark.parametrize("setting", SETTINGS)
def test_raw_payload_children_are_not_recorded_under_any_hide_setting(setting):
    assert config(**{setting: True}).mask(SpanAttributes.RAW_OUTPUT + ".0", PAYLOAD) is None


@pytest.mark.parametrize("setting", SETTINGS)
def test_unrelated_attributes_are_still_recorded(setting):
    assert config(**{setting: True}).mask("llm.model_name", "model-x") == "model-x"
