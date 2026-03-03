"""Tests for Pydantic AI agent wrapper."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio


class TestSafeSerialize:
    """Test safe_serialize function."""

    def test_serialize_none(self):
        """Test serializing None."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize(None) == ""

    def test_serialize_string(self):
        """Test serializing string."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize("hello") == "hello"

    def test_serialize_dict(self):
        """Test serializing dict."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        result = safe_serialize({"key": "value"})
        assert "key" in result
        assert "value" in result

    def test_serialize_list(self):
        """Test serializing list."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        result = safe_serialize([1, 2, 3])
        assert "1" in result
        assert "2" in result
        assert "3" in result

    def test_serialize_truncates_long_string(self):
        """Test that long strings are truncated."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        long_string = "a" * 5000
        result = safe_serialize(long_string, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_serialize_complex_object(self):
        """Test serializing complex object."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        obj = {"nested": {"key": [1, 2, {"deep": "value"}]}}
        result = safe_serialize(obj)
        assert "nested" in result
        assert "deep" in result


class TestExtractModelInfo:
    """Test extract_model_info function."""

    def test_extract_string_model(self):
        """Test extracting model from string."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        agent = MagicMock()
        agent.model = "openai:gpt-4o"

        info = extract_model_info(agent)

        assert info["model_name"] == "openai:gpt-4o"
        assert info["model_provider"] == "openai"

    def test_extract_model_object(self):
        """Test extracting model from object with model_name."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        model = MagicMock()
        model.model_name = "claude-3-opus"

        agent = MagicMock()
        agent.model = model

        info = extract_model_info(agent)

        assert info["model_name"] == "claude-3-opus"
        assert info["model_provider"] == "anthropic"

    def test_extract_no_model(self):
        """Test extracting when no model."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        agent = MagicMock()
        agent.model = None

        info = extract_model_info(agent)

        assert info == {}


class TestExtractUsage:
    """Test extract_usage function."""

    def test_extract_usage_with_data(self):
        """Test extracting usage data."""
        from traceai_pydantic_ai._agent_wrapper import extract_usage

        usage = MagicMock()
        usage.request_tokens = 100
        usage.response_tokens = 50
        usage.total_tokens = 150
        usage.requests = 1

        result = MagicMock()
        result.usage = usage

        extracted = extract_usage(result)

        assert extracted["input_tokens"] == 100
        assert extracted["output_tokens"] == 50
        assert extracted["total_tokens"] == 150

    def test_extract_usage_none(self):
        """Test extracting when no usage."""
        from traceai_pydantic_ai._agent_wrapper import extract_usage

        result = MagicMock()
        result.usage = None

        extracted = extract_usage(result)
        assert extracted is None

    def test_extract_usage_alternate_field_names(self):
        """Test extracting with alternate field names."""
        from traceai_pydantic_ai._agent_wrapper import extract_usage

        usage = MagicMock()
        usage.request_tokens = None
        usage.input_tokens = 100
        usage.response_tokens = None
        usage.output_tokens = 50
        usage.total_tokens = 150

        result = MagicMock()
        result.usage = usage

        extracted = extract_usage(result)

        assert extracted["input_tokens"] == 100
        assert extracted["output_tokens"] == 50


class TestWrapAgentRun:
    """Test wrap_agent_run function."""

    def test_creates_callable(self):
        """Test that wrap_agent_run returns a callable."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        mock_tracer = MagicMock()

        async def original_method(self, prompt):
            return MagicMock()

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")
        assert callable(wrapped)

    def test_sync_wrapper_is_sync(self):
        """Test that sync wrapper is synchronous."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        def original_method(self, prompt):
            return MagicMock()

        wrapped = wrap_agent_run(original_method, mock_tracer, "run_sync")

        # Should not be a coroutine function
        assert not asyncio.iscoroutinefunction(wrapped)

    @pytest.mark.asyncio
    async def test_async_wrapper_creates_span(self):
        """Test that async wrapper creates span."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        mock_result = MagicMock()
        mock_result.usage = None
        mock_result.output = "Hello"

        async def original_method(self, prompt):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = None

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")

        result = await wrapped(mock_agent, "Hello")

        mock_tracer.start_as_current_span.assert_called_once()
        assert result is mock_result

    @pytest.mark.asyncio
    async def test_async_wrapper_handles_error(self):
        """Test that async wrapper handles errors."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        async def original_method(self, prompt):
            raise ValueError("Test error")

        mock_agent = MagicMock()
        mock_agent.model = None

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")

        with pytest.raises(ValueError):
            await wrapped(mock_agent, "Hello")

        mock_span.record_exception.assert_called_once()


class TestWrapToolFunction:
    """Test wrap_tool_function function."""

    def test_creates_callable(self):
        """Test that wrap_tool_function returns a callable."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()

        def original_tool(ctx, arg1):
            return "result"

        wrapped = wrap_tool_function(original_tool, mock_tracer, "my_tool")
        assert callable(wrapped)

    def test_sync_tool_wrapping(self):
        """Test wrapping synchronous tool."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        def original_tool(ctx, arg1):
            return "result"

        wrapped = wrap_tool_function(original_tool, mock_tracer, "my_tool")

        result = wrapped(MagicMock(), "arg_value")

        assert result == "result"
        mock_tracer.start_as_current_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_tool_wrapping(self):
        """Test wrapping async tool."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        async def original_tool(ctx, arg1):
            return "async_result"

        wrapped = wrap_tool_function(original_tool, mock_tracer, "async_tool")

        result = await wrapped(MagicMock(), "arg_value")

        assert result == "async_result"
        mock_tracer.start_as_current_span.assert_called_once()

    def test_tool_with_description(self):
        """Test tool with description."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        def original_tool(ctx):
            return "result"

        wrapped = wrap_tool_function(
            original_tool,
            mock_tracer,
            "my_tool",
            tool_description="A helpful tool",
        )

        result = wrapped(MagicMock())

        assert result == "result"


class TestSpanNames:
    """Test span name constants."""

    def test_agent_run_span_name(self):
        """Test agent run span name."""
        from traceai_pydantic_ai._agent_wrapper import AGENT_RUN_SPAN_NAME

        assert AGENT_RUN_SPAN_NAME == "pydantic_ai.agent.run"

    def test_model_request_span_name(self):
        """Test model request span name."""
        from traceai_pydantic_ai._agent_wrapper import MODEL_REQUEST_SPAN_NAME

        assert MODEL_REQUEST_SPAN_NAME == "pydantic_ai.model.request"

    def test_tool_call_span_name(self):
        """Test tool call span name."""
        from traceai_pydantic_ai._agent_wrapper import TOOL_CALL_SPAN_NAME

        assert TOOL_CALL_SPAN_NAME == "pydantic_ai.tool.call"
