"""Use real core constants, serialization and exported-span privacy."""
import json

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from traceai_huggingface._response_attributes_extractor import _TextGenerationResponseAttributesExtractor


@pytest.mark.parametrize("hide_outputs", [False, True])
def test_exported_output_contract(export_attributes, hide_outputs):
    response = {'generated_text': 'private generated reply', 'details': {'generated_tokens': 5}}
    extractor = _TextGenerationResponseAttributesExtractor()
    attributes = dict(extractor.get_attributes(response))
    attributes.update(extractor.get_extra_attributes(response, {}))
    captured = export_attributes(attributes, hide_outputs=hide_outputs)
    if hide_outputs:
        assert captured[SpanAttributes.OUTPUT_VALUE] == REDACTED_VALUE
        assert not any(key.startswith(SpanAttributes.GEN_AI_OUTPUT_MESSAGES) for key in captured)
        assert "private generated reply" not in json.dumps(captured)
    else:
        assert json.loads(captured[SpanAttributes.OUTPUT_VALUE]) == response
        assert captured["gen_ai.output.messages.0.message.content"] == "private generated reply"
    assert captured[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 5


@pytest.mark.parametrize("hide_inputs", [False, True])
def test_generation_system_prompt_privacy(export_attributes, hide_inputs):
    from traceai_huggingface._request_attributes_extractor import _TextGenerationRequestAttributesExtractor

    request = {"model": "test-model", "system_prompt": "private system instructions", "prompt": "private user prompt"}
    extractor = _TextGenerationRequestAttributesExtractor()
    attributes = dict(extractor.get_attributes_from_request(request))
    attributes.update(extractor.get_extra_attributes_from_request(request))
    captured = export_attributes(attributes, hide_inputs=hide_inputs)
    assert captured[SpanAttributes.GEN_AI_PROVIDER_NAME] == "huggingface"
    assert captured[SpanAttributes.GEN_AI_REQUEST_MODEL] == "test-model"
    if hide_inputs:
        assert captured[SpanAttributes.INPUT_VALUE] == REDACTED_VALUE
        assert not any(key.startswith(SpanAttributes.GEN_AI_INPUT_MESSAGES) for key in captured)
        assert "private system instructions" not in json.dumps(captured)
        assert "private user prompt" not in json.dumps(captured)
    else:
        assert captured["gen_ai.input.messages.0.message.role"] == "system"
        assert captured["gen_ai.input.messages.0.message.content"] == "private system instructions"
        assert captured["gen_ai.input.messages.1.message.role"] == "user"
        assert captured["gen_ai.input.messages.1.message.content"] == "private user prompt"
        assert captured[SpanAttributes.INPUT_VALUE] == "private user prompt"
