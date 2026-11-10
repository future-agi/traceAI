"""Legacy SDK dictionaries and typed models must share privacy/accounting rules."""

import json
from types import MappingProxyType, SimpleNamespace

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from traceai_vllm._wrappers import (
    _get_input_attributes,
    _get_output_attributes,
    _get_stream_output_attributes,
)


@pytest.mark.parametrize("objects", [False, True])
@pytest.mark.parametrize("hide_images", [False, True])
def test_typed_multimodal_parts(export_attributes, objects, hide_images):
    image = "https://example.invalid/synthetic-private-image"
    text = "Keep this text visible"
    parts = [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image}},
    ]
    if objects:
        parts = [SimpleNamespace(**parts[0]), SimpleNamespace(
            type="image_url", image_url=SimpleNamespace(url=image)
        )]
        message = SimpleNamespace(role="user", content=parts)
    else:
        message = MappingProxyType({"role": "user", "content": parts})
    attributes = _get_input_attributes("test", [message])
    assert "gen_ai.input.messages.0.message.content" not in attributes
    captured = export_attributes(attributes, hide_input_images=hide_images)
    assert text in json.dumps(captured)
    assert (image in json.dumps(captured)) == (not hide_images)
    # Attribute projection must not replace or redact the caller's values.
    assert (parts[1].image_url.url if objects else parts[1]["image_url"]["url"]) == image


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize("count", [0, 7, None, True, False, -1, "7", 1.5, float("nan"), float("inf")])
def test_usage_validates_token_counts(stream, mapping, count):
    values = {"prompt_tokens": count, "completion_tokens": 5, "total_tokens": 8}
    usage = MappingProxyType(values) if mapping else SimpleNamespace(**values)
    response = SimpleNamespace(model="test", usage=usage)
    attrs = (_get_stream_output_attributes([response], "hello") if stream
             else _get_output_attributes(response))
    if type(count) is int and count >= 0:
        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == count
    else:
        assert SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS not in attrs
    assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 8


@pytest.mark.parametrize("usage", [None, {}, SimpleNamespace()])
def test_missing_usage_is_not_invented(usage):
    attrs = _get_stream_output_attributes([SimpleNamespace(usage=usage)], "")
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)
