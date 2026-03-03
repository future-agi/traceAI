"""Tests for the callback handler module."""

import pytest
from unittest.mock import MagicMock

from traceai_strands._callback import (
    StrandsCallbackHandler,
    safe_serialize,
    create_callback_handler,
)


class TestSafeSerialize:
    """Tests for safe_serialize function."""

    def test_serialize_none(self):
        """Test serializing None."""
        assert safe_serialize(None) == "null"

    def test_serialize_string(self):
        """Test serializing string."""
        assert safe_serialize("hello") == "hello"

    def test_serialize_dict(self):
        """Test serializing dict."""
        result = safe_serialize({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_serialize_list(self):
        """Test serializing list."""
        result = safe_serialize([1, 2, 3])
        assert result == "[1, 2, 3]"

    def test_serialize_truncates_long_strings(self):
        """Test truncation of long strings."""
        long_string = "x" * 20000
        result = safe_serialize(long_string)
        assert len(result) <= 10000
        assert result.endswith("...")

    def test_serialize_with_custom_max_length(self):
        """Test custom max length."""
        long_string = "x" * 200
        result = safe_serialize(long_string, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_serialize_complex_object(self):
        """Test serializing complex object."""
        obj = MagicMock()
        result = safe_serialize(obj)
        assert isinstance(result, str)


class TestStrandsCallbackHandler:
    """Tests for StrandsCallbackHandler class."""

    def test_initialization(self, mock_tracer_provider):
        """Test handler initialization."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        assert handler._capture_input is True
        assert handler._capture_output is True

    def test_initialization_with_options(self, mock_tracer_provider):
        """Test handler initialization with options."""
        handler = StrandsCallbackHandler(
            tracer_provider=mock_tracer_provider,
            capture_input=False,
            capture_output=False,
        )

        assert handler._capture_input is False
        assert handler._capture_output is False

    def test_on_agent_start(self, mock_tracer_provider, mock_agent):
        """Test on_agent_start callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_agent_start(mock_agent, "Hello!")

        assert handler._agent_span is not None
        assert handler._start_time is not None

    def test_on_agent_end(self, mock_tracer_provider, mock_agent, mock_response):
        """Test on_agent_end callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_agent_start(mock_agent, "Hello!")
        handler.on_agent_end(mock_agent, mock_response)

        assert handler._agent_span is None
        assert handler._start_time is None

    def test_on_agent_error(self, mock_tracer_provider, mock_agent):
        """Test on_agent_error callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_agent_start(mock_agent, "Hello!")
        handler.on_agent_error(mock_agent, ValueError("Test error"))

        assert handler._agent_span is None

    def test_on_tool_start(self, mock_tracer_provider, mock_tool):
        """Test on_tool_start callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_tool_start(mock_tool, {"x": 1, "y": 2})

        assert "tool:calculator" in handler._active_spans

    def test_on_tool_end(self, mock_tracer_provider, mock_tool):
        """Test on_tool_end callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_tool_start(mock_tool, {"x": 1, "y": 2})
        handler.on_tool_end(mock_tool, 3)

        assert "tool:calculator" not in handler._active_spans

    def test_on_tool_error(self, mock_tracer_provider, mock_tool):
        """Test on_tool_error callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_tool_start(mock_tool, {"x": 1, "y": 2})
        handler.on_tool_error(mock_tool, ValueError("Division by zero"))

        assert "tool:calculator" not in handler._active_spans

    def test_on_model_start(self, mock_tracer_provider):
        """Test on_model_start callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        model = MagicMock()
        model.model_id = "gpt-4"

        handler.on_model_start(model, [{"role": "user", "content": "Hello"}])

        assert "model:gpt-4" in handler._active_spans

    def test_on_model_end(self, mock_tracer_provider, mock_response):
        """Test on_model_end callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        model = MagicMock()
        model.model_id = "gpt-4"

        handler.on_model_start(model, [{"role": "user", "content": "Hello"}])
        handler.on_model_end(model, mock_response)

        assert "model:gpt-4" not in handler._active_spans

    def test_on_model_error(self, mock_tracer_provider):
        """Test on_model_error callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        model = MagicMock()
        model.model_id = "gpt-4"

        handler.on_model_start(model, [{"role": "user", "content": "Hello"}])
        handler.on_model_error(model, ValueError("API error"))

        assert "model:gpt-4" not in handler._active_spans

    def test_on_streaming_start(self, mock_tracer_provider, mock_agent):
        """Test on_streaming_start callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_streaming_start(mock_agent)

        assert "stream" in handler._active_spans

    def test_on_streaming_end(self, mock_tracer_provider, mock_agent, mock_response):
        """Test on_streaming_end callback."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        handler.on_streaming_start(mock_agent)
        handler.on_streaming_end(mock_agent, mock_response)

        assert "stream" not in handler._active_spans

    def test_callable_interface(self, mock_tracer_provider):
        """Test callable interface for Strands callbacks."""
        handler = StrandsCallbackHandler(tracer_provider=mock_tracer_provider)

        # Test unknown event
        result = handler(event="unknown_event", data={"key": "value"})

        # Should handle gracefully
        assert result is None

    def test_capture_input_disabled(self, mock_tracer_provider, mock_agent):
        """Test that input is not captured when disabled."""
        handler = StrandsCallbackHandler(
            tracer_provider=mock_tracer_provider,
            capture_input=False,
        )

        handler.on_agent_start(mock_agent, "Hello!")

        # Input should not be in span attributes
        # (actual attribute checking would require more detailed mock)
        assert handler._agent_span is not None

    def test_capture_output_disabled(self, mock_tracer_provider, mock_agent, mock_response):
        """Test that output is not captured when disabled."""
        handler = StrandsCallbackHandler(
            tracer_provider=mock_tracer_provider,
            capture_output=False,
        )

        handler.on_agent_start(mock_agent, "Hello!")
        handler.on_agent_end(mock_agent, mock_response)

        assert handler._agent_span is None


class TestCreateCallbackHandler:
    """Tests for create_callback_handler function."""

    def test_creates_handler(self, mock_tracer_provider):
        """Test creating a callback handler."""
        handler = create_callback_handler(tracer_provider=mock_tracer_provider)

        assert isinstance(handler, StrandsCallbackHandler)

    def test_creates_handler_with_options(self, mock_tracer_provider):
        """Test creating a handler with options."""
        handler = create_callback_handler(
            tracer_provider=mock_tracer_provider,
            capture_input=False,
            capture_output=False,
        )

        assert handler._capture_input is False
        assert handler._capture_output is False
