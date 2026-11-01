"""Use real core constants, serialization and exported-span privacy."""
import json

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from traceai_cohere._response_attributes_extractor import _ChatResponseAttributesExtractor


@pytest.mark.parametrize("hide_outputs", [False, True])
def test_exported_output_contract(export_attributes, hide_outputs):
    response = {'text': 'private generated reply', 'meta': {'billed_units': {'input_tokens': 3, 'output_tokens': 5}}}
    extractor = _ChatResponseAttributesExtractor()
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
    from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

    request = {'model': 'command-r', 'preamble': 'private system instructions', 'chat_history': [{'role': 'USER', 'message': 'previous question'}, {'role': 'CHATBOT', 'message': 'previous reply'}], 'message': 'current question'}
    extractor = _ChatRequestAttributesExtractor()
    attributes = dict(extractor.get_attributes_from_request(request))
    attributes.update(extractor.get_extra_attributes_from_request(request))
    captured = export_attributes(attributes, hide_inputs=hide_inputs)
    assert captured[SpanAttributes.GEN_AI_PROVIDER_NAME] == "cohere"
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
        assert captured["gen_ai.input.messages.1.message.content"] == "previous question"
        assert captured["gen_ai.input.messages.2.message.content"] == "previous reply"
        assert captured["gen_ai.input.messages.3.message.content"] == "current question"
