"""Serialization and real-core privacy contracts; no provider requests."""
import json

import pytest
from fi_instrumentation.fi_types import SpanAttributes, MessageAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from traceai_cerebras._wrappers import _get_output_attributes, _get_stream_output_attributes


@pytest.mark.parametrize("hide_outputs", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_exported_output_contract(
    export_attributes, mock_completion, mock_stream_chunks, hide_outputs, streaming
):
    if streaming:
        content = "".join(chunk.choices[0].delta.content or "" for chunk in mock_stream_chunks)
        attrs = _get_stream_output_attributes(mock_stream_chunks, content)
        payload = [chunk.model_dump() for chunk in mock_stream_chunks]
    else:
        content = mock_completion.choices[0].message.content
        attrs = _get_output_attributes(mock_completion)
        payload = mock_completion.model_dump()

    captured = export_attributes(attrs, hide_outputs=hide_outputs)
    content_key = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
    if hide_outputs:
        assert captured[SpanAttributes.OUTPUT_VALUE] == REDACTED_VALUE
        assert not any(key.startswith(SpanAttributes.GEN_AI_OUTPUT_MESSAGES) for key in captured)
        assert content not in json.dumps(captured)
    else:
        assert json.loads(captured[SpanAttributes.OUTPUT_VALUE]) == payload
        assert captured[content_key] == content
    # Masking content must not throw away accounting or model attribution.
    assert captured[SpanAttributes.GEN_AI_REQUEST_MODEL] == attrs[SpanAttributes.GEN_AI_REQUEST_MODEL]
    assert captured[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS]
