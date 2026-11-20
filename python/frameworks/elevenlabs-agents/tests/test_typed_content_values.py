import json

import pytest
from fi_instrumentation import TraceConfig

from traceai_elevenlabs_agents.map import map_otlp_traces

TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "0123456789abcdef"

TYPED_VALUES = {
    "int": ({"intValue": "4242"}, "4242"),
    "double": ({"doubleValue": 0.4244}, "0.4244"),
    "bool": ({"boolValue": True}, "true"),
}

# (hide setting, content key it governs)
CASES = [
    ("hide_input_images", "input.value"),
    ("hide_input_images", "gen_ai.input.images"),
    ("hide_llm_invocation_parameters", "gen_ai.request.parameters"),
]


def _payload(key, any_value):
    span = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "name": "custom.step",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": [{"key": key, "value": any_value}],
    }
    return {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [span]}]}]}


def _exported(key, any_value, config):
    request, _ = map_otlp_traces(
        _payload(key, any_value), conversation_id="conv_typed", project_name="p", trace_config=config
    )
    return json.dumps(request)


@pytest.mark.parametrize("kind", sorted(TYPED_VALUES))
@pytest.mark.parametrize("setting,key", CASES)
def test_typed_content_values_are_exported_without_hide_settings(setting, key, kind):
    any_value, needle = TYPED_VALUES[kind]
    assert needle in _exported(key, any_value, TraceConfig())


@pytest.mark.parametrize("kind", sorted(TYPED_VALUES))
@pytest.mark.parametrize("setting,key", CASES)
def test_typed_content_values_are_masked_under_hide_settings(setting, key, kind):
    any_value, needle = TYPED_VALUES[kind]
    blob = _exported(key, any_value, TraceConfig(**{setting: True}))
    assert needle not in blob
