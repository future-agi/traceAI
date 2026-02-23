"""Tests for Cerebras wrapper functions."""

import pytest
from unittest.mock import MagicMock, AsyncMock

from traceai_cerebras._wrappers import (
    _get_input_attributes,
    _get_output_attributes,
    _get_stream_output_attributes,
    _CompletionsWrapper,
    _AsyncCompletionsWrapper,
    CEREBRAS_QUEUE_TIME,
    CEREBRAS_PROMPT_TIME,
    CEREBRAS_COMPLETION_TIME,
    CEREBRAS_TOTAL_TIME,
)
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes, MessageAttributes


class TestGetInputAttributes:
    """Tests for _get_input_attributes function."""

    def test_basic_attributes(self):
        """Should include basic LLM attributes."""
        attrs = _get_input_attributes(
            model="llama3.1-8b",
            messages=[{"role": "user", "content": "Hello"}],
        )

        assert attrs[SpanAttributes.GEN_AI_SPAN_KIND] == FiSpanKindValues.LLM.value
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "cerebras"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "cerebras"
        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "llama3.1-8b"

    def test_message_attributes(self):
        """Should include message attributes."""
        attrs = _get_input_attributes(
            model="llama3.1-8b",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ],
        )

        assert attrs[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}"] == "system"
        assert attrs[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"] == "You are helpful."
        assert attrs[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_ROLE}"] == "user"
        assert attrs[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_CONTENT}"] == "Hello"

    def test_optional_parameters(self):
        """Should include optional parameters when provided."""
        attrs = _get_input_attributes(
            model="llama3.1-8b",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=100,
            temperature=0.7,
            top_p=0.9,
        )

        assert SpanAttributes.GEN_AI_REQUEST_PARAMETERS in attrs

    def test_raw_input_included(self):
        """Should include raw input as JSON."""
        attrs = _get_input_attributes(
            model="llama3.1-8b",
            messages=[{"role": "user", "content": "Hello"}],
        )

        assert SpanAttributes.INPUT_VALUE in attrs

    def test_handles_none_values(self):
        """Should skip None parameter values."""
        attrs = _get_input_attributes(
            model="llama3.1-8b",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=None,
            temperature=None,
        )

        assert SpanAttributes.GEN_AI_REQUEST_PARAMETERS not in attrs


class TestGetOutputAttributes:
    """Tests for _get_output_attributes function."""

    def test_basic_response_attributes(self, mock_completion):
        """Should extract basic response attributes."""
        attrs = _get_output_attributes(mock_completion)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "llama3.1-8b"

    def test_message_content(self, mock_completion):
        """Should extract message content."""
        attrs = _get_output_attributes(mock_completion)

        assert attrs[f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}"] == "assistant"
        assert attrs[f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"] == "Test response"
        assert attrs[SpanAttributes.OUTPUT_VALUE] == "Test response"

    def test_usage_metrics(self, mock_completion):
        """Should extract usage metrics."""
        attrs = _get_output_attributes(mock_completion)

        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 10
        assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 20
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 30

    def test_time_info_attributes(self, mock_completion):
        """Should extract Cerebras time_info attributes."""
        attrs = _get_output_attributes(mock_completion)

        assert CEREBRAS_QUEUE_TIME in attrs
        assert CEREBRAS_PROMPT_TIME in attrs
        assert CEREBRAS_COMPLETION_TIME in attrs
        assert CEREBRAS_TOTAL_TIME in attrs

    def test_raw_output_included(self, mock_completion):
        """Should include raw output."""
        attrs = _get_output_attributes(mock_completion)

        assert SpanAttributes.OUTPUT_VALUE in attrs


class TestGetStreamOutputAttributes:
    """Tests for _get_stream_output_attributes function."""

    def test_concatenated_content(self, mock_stream_chunks):
        """Should have full concatenated content."""
        full_content = "Hello there!"
        attrs = _get_stream_output_attributes(mock_stream_chunks, full_content)

        assert attrs[SpanAttributes.OUTPUT_VALUE] == full_content
        assert attrs[f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"] == full_content
        assert attrs[f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}"] == "assistant"

    def test_model_from_last_chunk(self, mock_stream_chunks):
        """Should get model from last chunk."""
        attrs = _get_stream_output_attributes(mock_stream_chunks, "Hello!")

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "llama3.1-8b"

    def test_usage_from_last_chunk(self, mock_stream_chunks):
        """Should get usage from last chunk."""
        attrs = _get_stream_output_attributes(mock_stream_chunks, "Hello!")

        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 10
        assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 20
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 30

    def test_time_info_from_last_chunk(self, mock_stream_chunks):
        """Should get time_info from last chunk."""
        attrs = _get_stream_output_attributes(mock_stream_chunks, "Hello!")

        assert CEREBRAS_QUEUE_TIME in attrs
        assert CEREBRAS_TOTAL_TIME in attrs

    def test_empty_chunks(self):
        """Should handle empty chunks list."""
        attrs = _get_stream_output_attributes([], "")

        assert attrs[SpanAttributes.OUTPUT_VALUE] == ""
        assert SpanAttributes.GEN_AI_REQUEST_MODEL not in attrs


class TestCompletionsWrapper:
    """Tests for _CompletionsWrapper class."""

    def test_non_streaming_call(self, mock_tracer, mock_completion):
        """Test non-streaming completion call."""
        wrapper = _CompletionsWrapper(tracer=mock_tracer)
        wrapped_fn = MagicMock(return_value=mock_completion)

        result = wrapper(
            wrapped_fn,
            None,
            (),
            {"model": "llama3.1-8b", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert result == mock_completion
        wrapped_fn.assert_called_once()

    def test_streaming_call(self, mock_tracer, mock_stream_chunks):
        """Test streaming completion call."""
        wrapper = _CompletionsWrapper(tracer=mock_tracer)
        wrapped_fn = MagicMock(return_value=iter(mock_stream_chunks))

        result = wrapper(
            wrapped_fn,
            None,
            (),
            {
                "model": "llama3.1-8b",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )

        chunks = list(result)
        assert len(chunks) == 5

    def test_error_handling(self, mock_tracer):
        """Test error handling in wrapper."""
        wrapper = _CompletionsWrapper(tracer=mock_tracer)
        wrapped_fn = MagicMock(side_effect=Exception("API Error"))

        with pytest.raises(Exception) as exc_info:
            wrapper(
                wrapped_fn,
                None,
                (),
                {"model": "llama3.1-8b", "messages": []},
            )

        assert "API Error" in str(exc_info.value)


class TestAsyncCompletionsWrapper:
    """Tests for _AsyncCompletionsWrapper class."""

    @pytest.mark.asyncio
    async def test_async_non_streaming_call(self, mock_tracer, mock_completion):
        """Test async non-streaming completion call."""
        wrapper = _AsyncCompletionsWrapper(tracer=mock_tracer)
        wrapped_fn = AsyncMock(return_value=mock_completion)

        result = await wrapper(
            wrapped_fn,
            None,
            (),
            {"model": "llama3.1-8b", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert result == mock_completion
        wrapped_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_error_handling(self, mock_tracer):
        """Test async error handling in wrapper."""
        wrapper = _AsyncCompletionsWrapper(tracer=mock_tracer)
        wrapped_fn = AsyncMock(side_effect=Exception("API Error"))

        with pytest.raises(Exception) as exc_info:
            await wrapper(
                wrapped_fn,
                None,
                (),
                {"model": "llama3.1-8b", "messages": []},
            )

        assert "API Error" in str(exc_info.value)


class TestCerebrasSpecificAttributes:
    """Tests for Cerebras-specific attributes."""

    def test_time_info_constants(self):
        """Test that Cerebras time_info constants are defined."""
        assert CEREBRAS_QUEUE_TIME == "cerebras.queue_time"
        assert CEREBRAS_PROMPT_TIME == "cerebras.prompt_time"
        assert CEREBRAS_COMPLETION_TIME == "cerebras.completion_time"
        assert CEREBRAS_TOTAL_TIME == "cerebras.total_time"

    def test_time_info_in_response(self, mock_completion):
        """Test time_info extraction from response."""
        attrs = _get_output_attributes(mock_completion)

        assert attrs[CEREBRAS_QUEUE_TIME] == 0.001
        assert attrs[CEREBRAS_PROMPT_TIME] == 0.010
        assert attrs[CEREBRAS_COMPLETION_TIME] == 0.050
        assert attrs[CEREBRAS_TOTAL_TIME] == 0.061
