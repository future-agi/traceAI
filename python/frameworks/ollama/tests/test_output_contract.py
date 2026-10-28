"""Use real core constants, serialization and exported-span privacy."""
import json

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor


@pytest.mark.parametrize("hide_outputs", [False, True])
def test_exported_output_contract(export_attributes, hide_outputs):
    response = {'response': 'private generated reply', 'prompt_eval_count': 3, 'eval_count': 5}
    extractor = _ResponseAttributesExtractor()
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


@pytest.mark.parametrize("hidden", [False, True])
def test_embedding_payload_and_vector_privacy(export_attributes, hidden):
    response = {"embeddings": [[float(i) for i in range(12)], [0.5] * 12]}
    attributes = dict(_ResponseAttributesExtractor().get_extra_attributes(response, {}))
    captured = export_attributes(attributes, hide_outputs=hidden, hide_embedding_vectors=hidden)
    if hidden:
        assert captured[SpanAttributes.OUTPUT_VALUE] == REDACTED_VALUE
        assert not any("embedding.vector" in key for key in captured)
    else:
        payload = json.loads(captured[SpanAttributes.OUTPUT_VALUE])
        assert payload == response
        assert len(payload["embeddings"]) == 2
        assert len(payload["embeddings"][0]) == 12
        assert json.loads(captured["embedding.embeddings.0.embedding.vector"]) == response["embeddings"][0][:10]
        assert json.loads(captured["embedding.embeddings.1.embedding.vector"]) == response["embeddings"][1][:10]


@pytest.mark.parametrize("hide_inputs", [False, True])
def test_generation_system_prompt_privacy(export_attributes, hide_inputs):
    from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor

    request = {"model": "test-model", "system": "private system instructions", "prompt": "private user prompt"}
    extractor = _RequestAttributesExtractor()
    attributes = dict(extractor.get_attributes_from_request(request))
    attributes.update(extractor.get_extra_attributes_from_request(request))
    captured = export_attributes(attributes, hide_inputs=hide_inputs)
    assert captured[SpanAttributes.GEN_AI_PROVIDER_NAME] == "ollama"
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
