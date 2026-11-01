"""Use real core constants, serialization and exported-span privacy."""
import json

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor


@pytest.mark.parametrize("hide_outputs", [False, True])
def test_exported_output_contract(export_attributes, hide_outputs):
    response = {'choices': [{'message': {'role': 'assistant', 'content': 'private generated reply'}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 5, 'total_tokens': 8}}
    extractor = _ChatCompletionResponseAttributesExtractor()
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
def test_system_message_is_preserved_and_masked(export_attributes, hide_inputs):
    from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

    request = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': 'private system instructions'}, {'role': 'user', 'content': 'current question'}]}
    extractor = _ChatCompletionRequestAttributesExtractor()
    attributes = dict(extractor.get_attributes_from_request(request))
    attributes.update(extractor.get_extra_attributes_from_request(request))
    captured = export_attributes(attributes, hide_inputs=hide_inputs)
    assert captured[SpanAttributes.GEN_AI_PROVIDER_NAME] == "deepseek"
    assert captured[SpanAttributes.GEN_AI_REQUEST_MODEL] == request["model"]
    if hide_inputs:
        assert captured[SpanAttributes.INPUT_VALUE] == REDACTED_VALUE
        assert not any(key.startswith(SpanAttributes.GEN_AI_INPUT_MESSAGES) for key in captured)
        for content in ["private system instructions", "current question", "previous question", "previous reply"]:
            assert content not in json.dumps(captured)
    else:
        assert captured["gen_ai.input.messages.0.message.role"] == "system"
        assert captured["gen_ai.input.messages.0.message.content"] == "private system instructions"
        assert captured[SpanAttributes.INPUT_VALUE] == "current question"
        assert captured["gen_ai.input.messages.1.message.content"] == "current question"
