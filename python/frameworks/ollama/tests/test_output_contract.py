"""Use real core constants, serialization and exported-span privacy."""
import json

import pytest
from fi_instrumentation import TraceConfig
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


@pytest.mark.parametrize("hide_outputs", [False, True])
@pytest.mark.parametrize("hide_embedding_vectors", [False, True])
@pytest.mark.parametrize("multiple", [False, True])
def test_embedding_payload_and_vector_privacy(
    export_attributes, hide_outputs, hide_embedding_vectors, multiple
):
    vectors = [[0.314159 + i for i in range(12)], [0.577215] * 12]
    key = "embeddings" if multiple else "embedding"
    response = {key: vectors if multiple else vectors[0]}
    privacy = dict(hide_outputs=hide_outputs, hide_embedding_vectors=hide_embedding_vectors)
    extractor = _ResponseAttributesExtractor(config=TraceConfig(**privacy))
    attributes = dict(extractor.get_extra_attributes(response, {}))
    captured = export_attributes(attributes, **privacy)
    if hide_outputs:
        assert captured[SpanAttributes.OUTPUT_VALUE] == REDACTED_VALUE
    else:
        payload = json.loads(captured[SpanAttributes.OUTPUT_VALUE])
        assert payload == ({key: REDACTED_VALUE} if hide_embedding_vectors else response)
        if not hide_embedding_vectors:
            if multiple:
                assert len(payload["embeddings"]) == 2
                assert len(payload["embeddings"][0]) == 12
            else:
                assert len(payload["embedding"]) == 12
    if hide_embedding_vectors:
        assert not any("embedding.vector" in key for key in captured)
        assert "0.314159" not in json.dumps(captured)
        assert "0.577215" not in json.dumps(captured)
    else:
        assert json.loads(captured["embedding.embeddings.0.embedding.vector"]) == vectors[0][:10]
        if multiple:
            assert json.loads(captured["embedding.embeddings.1.embedding.vector"]) == vectors[1][:10]
    assert response[key] == (vectors if multiple else vectors[0])


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
