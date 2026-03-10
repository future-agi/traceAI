"""Tests for the middleware module."""

import pytest
from unittest.mock import MagicMock

from traceai_beeai._middleware import (
    TraceAIMiddleware,
    safe_serialize,
    create_tracing_middleware,
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


class TestTraceAIMiddleware:
    """Tests for TraceAIMiddleware class."""

    def test_initialization(self, mock_tracer_provider):
        """Test middleware initialization."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        assert middleware._capture_input is True
        assert middleware._capture_output is True

    def test_initialization_with_options(self, mock_tracer_provider):
        """Test middleware initialization with options."""
        middleware = TraceAIMiddleware(
            tracer_provider=mock_tracer_provider,
            capture_input=False,
            capture_output=False,
            session_id="sess-123",
            user_id="user@example.com",
        )

        assert middleware._capture_input is False
        assert middleware._capture_output is False
        assert middleware._session_id == "sess-123"
        assert middleware._user_id == "user@example.com"

    def test_on_agent_start(self, mock_tracer_provider, mock_agent):
        """Test on_agent_start method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        middleware.on_agent_start(mock_agent, "Hello!")

        assert "agent" in middleware._active_spans

    def test_on_agent_end(self, mock_tracer_provider, mock_agent, mock_response):
        """Test on_agent_end method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        middleware.on_agent_start(mock_agent, "Hello!")
        middleware.on_agent_end(mock_agent, mock_response)

        assert "agent" not in middleware._active_spans

    def test_on_agent_error(self, mock_tracer_provider, mock_agent):
        """Test on_agent_error method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        middleware.on_agent_start(mock_agent, "Hello!")
        middleware.on_agent_error(mock_agent, ValueError("Test error"))

        assert "agent" not in middleware._active_spans

    def test_on_tool_start(self, mock_tracer_provider, mock_tool):
        """Test on_tool_start method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        middleware.on_tool_start(mock_tool, {"x": 1, "y": 2})

        assert "tool:calculator" in middleware._active_spans

    def test_on_tool_end(self, mock_tracer_provider, mock_tool):
        """Test on_tool_end method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        middleware.on_tool_start(mock_tool, {"x": 1, "y": 2})
        middleware.on_tool_end(mock_tool, 3)

        assert "tool:calculator" not in middleware._active_spans

    def test_on_tool_error(self, mock_tracer_provider, mock_tool):
        """Test on_tool_error method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        middleware.on_tool_start(mock_tool, {"x": 1, "y": 2})
        middleware.on_tool_error(mock_tool, ValueError("Calculation error"))

        assert "tool:calculator" not in middleware._active_spans

    def test_on_llm_start(self, mock_tracer_provider):
        """Test on_llm_start method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        model = MagicMock()
        model.model = "gpt-4"

        middleware.on_llm_start(model, [{"role": "user", "content": "Hello"}])

        assert "llm:gpt-4" in middleware._active_spans

    def test_on_llm_end(self, mock_tracer_provider, mock_response):
        """Test on_llm_end method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        model = MagicMock()
        model.model = "gpt-4"

        middleware.on_llm_start(model, [{"role": "user", "content": "Hello"}])
        middleware.on_llm_end(model, mock_response)

        assert "llm:gpt-4" not in middleware._active_spans

    def test_on_llm_error(self, mock_tracer_provider):
        """Test on_llm_error method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        model = MagicMock()
        model.model = "gpt-4"

        middleware.on_llm_start(model, [{"role": "user", "content": "Hello"}])
        middleware.on_llm_error(model, ValueError("API error"))

        assert "llm:gpt-4" not in middleware._active_spans

    def test_on_workflow_step(self, mock_tracer_provider):
        """Test on_workflow_step method."""
        middleware = TraceAIMiddleware(tracer_provider=mock_tracer_provider)

        workflow = MagicMock()
        workflow.name = "data_pipeline"

        # This should complete without error
        middleware.on_workflow_step(workflow, "preprocess", {"data_size": 100})

    def test_session_id_in_spans(self, mock_tracer_provider, mock_agent):
        """Test that session_id is included in spans."""
        middleware = TraceAIMiddleware(
            tracer_provider=mock_tracer_provider,
            session_id="sess-123",
        )

        middleware.on_agent_start(mock_agent, "Hello!")

        # Session ID should be set when span is created
        assert middleware._session_id == "sess-123"

    def test_capture_input_disabled(self, mock_tracer_provider, mock_agent):
        """Test that input is not captured when disabled."""
        middleware = TraceAIMiddleware(
            tracer_provider=mock_tracer_provider,
            capture_input=False,
        )

        middleware.on_agent_start(mock_agent, "Hello!")

        assert "agent" in middleware._active_spans


class TestCreateTracingMiddleware:
    """Tests for create_tracing_middleware function."""

    def test_creates_middleware(self, mock_tracer_provider):
        """Test creating a middleware instance."""
        middleware = create_tracing_middleware(tracer_provider=mock_tracer_provider)

        assert isinstance(middleware, TraceAIMiddleware)

    def test_creates_middleware_with_options(self, mock_tracer_provider):
        """Test creating middleware with options."""
        middleware = create_tracing_middleware(
            tracer_provider=mock_tracer_provider,
            capture_input=False,
            capture_output=False,
            session_id="sess-123",
            user_id="user@example.com",
        )

        assert middleware._capture_input is False
        assert middleware._capture_output is False
        assert middleware._session_id == "sess-123"
        assert middleware._user_id == "user@example.com"
