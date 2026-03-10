"""Extended tests for Pydantic AI agent wrapper."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import json


class TestSafeSerializeEdgeCases:
    """Test edge cases for safe_serialize function."""

    def test_serialize_bytes(self):
        """Test serializing bytes."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        result = safe_serialize(b"hello bytes")
        assert "hello" in result or "bytes" in result

    def test_serialize_int(self):
        """Test serializing integer."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize(42) == "42"

    def test_serialize_float(self):
        """Test serializing float."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize(3.14) == "3.14"

    def test_serialize_bool(self):
        """Test serializing boolean."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize(True) == "True"
        assert safe_serialize(False) == "False"

    def test_serialize_nested_dict(self):
        """Test serializing nested dictionary."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        nested = {"level1": {"level2": {"level3": "deep"}}}
        result = safe_serialize(nested)
        assert "level1" in result
        assert "level2" in result
        assert "level3" in result
        assert "deep" in result

    def test_serialize_with_special_chars(self):
        """Test serializing with special characters."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        special = "Hello\nWorld\t\"quoted\""
        result = safe_serialize(special)
        assert result == special

    def test_serialize_unicode(self):
        """Test serializing unicode strings."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        unicode_str = "Hello 世界 🌍"
        result = safe_serialize(unicode_str)
        assert result == unicode_str

    def test_serialize_empty_dict(self):
        """Test serializing empty dict."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize({}) == "{}"

    def test_serialize_empty_list(self):
        """Test serializing empty list."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        assert safe_serialize([]) == "[]"

    def test_serialize_mixed_list(self):
        """Test serializing mixed type list."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        mixed = [1, "two", 3.0, {"four": 4}]
        result = safe_serialize(mixed)
        assert "1" in result
        assert "two" in result
        assert "3.0" in result
        assert "four" in result

    def test_serialize_class_instance(self):
        """Test serializing class instance."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        class MyClass:
            def __init__(self):
                self.value = 42

        obj = MyClass()
        result = safe_serialize(obj)
        assert "MyClass" in result or "42" in str(result)

    def test_serialize_truncation_boundary(self):
        """Test truncation at exact boundary."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        # Create string exactly at max length
        exactly_max = "a" * 100
        result = safe_serialize(exactly_max, max_length=100)
        assert len(result) == 100
        assert not result.endswith("...")

    def test_serialize_truncation_one_over(self):
        """Test truncation one over boundary."""
        from traceai_pydantic_ai._agent_wrapper import safe_serialize

        # Create string one over max length
        one_over = "a" * 101
        result = safe_serialize(one_over, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")


class TestExtractModelInfoEdgeCases:
    """Test edge cases for extract_model_info function."""

    def test_model_with_class_name(self):
        """Test model with __class__.__name__."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        class FakeModel:
            pass

        agent = MagicMock()
        agent.model = FakeModel()

        info = extract_model_info(agent)
        # Should handle gracefully when no name attribute
        assert isinstance(info, dict)

    def test_model_with_empty_string(self):
        """Test model with empty string."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        agent = MagicMock()
        agent.model = ""

        info = extract_model_info(agent)
        # Empty string is falsy, so no model info is extracted
        assert info == {}

    def test_model_with_model_name_attr(self):
        """Test model object with model_name attribute."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        model = MagicMock()
        model.model_name = "gpt-4-turbo-preview"

        agent = MagicMock()
        agent.model = model

        info = extract_model_info(agent)
        assert info["model_name"] == "gpt-4-turbo-preview"
        assert info["model_provider"] == "openai"

    def test_model_with_name_attr(self):
        """Test model object with name attribute."""
        from traceai_pydantic_ai._agent_wrapper import extract_model_info

        model = MagicMock(spec=["name"])
        model.name = "claude-3-5-sonnet"
        # Mock that model_name doesn't exist
        del model.model_name

        agent = MagicMock()
        agent.model = model

        info = extract_model_info(agent)
        assert info["model_name"] == "claude-3-5-sonnet"


class TestExtractUsageEdgeCases:
    """Test edge cases for extract_usage function."""

    def test_usage_with_only_total_tokens(self):
        """Test usage with only total tokens."""
        from traceai_pydantic_ai._agent_wrapper import extract_usage

        usage = MagicMock()
        usage.request_tokens = None
        usage.response_tokens = None
        usage.input_tokens = None
        usage.output_tokens = None
        usage.total_tokens = 1000
        usage.requests = None

        result = MagicMock()
        result.usage = usage

        extracted = extract_usage(result)
        assert extracted["total_tokens"] == 1000

    def test_usage_with_zero_tokens(self):
        """Test usage with zero tokens."""
        from traceai_pydantic_ai._agent_wrapper import extract_usage

        # Create usage object with actual values (not MagicMock auto-values)
        class Usage:
            request_tokens = 0
            response_tokens = 0
            total_tokens = 0
            requests = 0

        result = MagicMock()
        result.usage = Usage()

        extracted = extract_usage(result)
        assert extracted["input_tokens"] == 0
        assert extracted["output_tokens"] == 0
        assert extracted["total_tokens"] == 0

    def test_usage_attribute_error(self):
        """Test usage when attributes raise errors."""
        from traceai_pydantic_ai._agent_wrapper import extract_usage

        result = MagicMock()
        result.usage = None

        extracted = extract_usage(result)
        assert extracted is None


class TestWrapAgentRunExtended:
    """Extended tests for wrap_agent_run function."""

    @pytest.mark.asyncio
    async def test_async_wrapper_with_message_history(self):
        """Test async wrapper with message history."""
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
        mock_result.output = "response"

        async def original_method(self, prompt, message_history=None):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = None

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")

        # Pass message history
        history = [{"role": "user", "content": "hello"}]
        await wrapped(mock_agent, "Hi", message_history=history)

        # Verify span was created with message history length
        mock_tracer.start_as_current_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_wrapper_with_structured_output(self):
        """Test async wrapper with structured output type."""
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
        mock_result.output = {"city": "Paris", "country": "France"}

        async def original_method(self, prompt):
            return mock_result

        class CityInfo:
            pass

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = CityInfo

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")
        await wrapped(mock_agent, "Tell me about Paris")

        mock_tracer.start_as_current_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_wrapper_with_dynamic_instructions(self):
        """Test async wrapper with callable instructions."""
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
        mock_result.output = "response"

        async def original_method(self, prompt):
            return mock_result

        def dynamic_instructions(ctx):
            return f"Be helpful for user {ctx.user_id}"

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = dynamic_instructions
        mock_agent.result_type = None

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")
        await wrapped(mock_agent, "Hello")

        mock_tracer.start_as_current_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_wrapper_with_usage_data(self):
        """Test async wrapper records usage data."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        usage = MagicMock()
        usage.request_tokens = 100
        usage.response_tokens = 50
        usage.total_tokens = 150

        mock_result = MagicMock()
        mock_result.usage = usage
        mock_result.output = "response"

        async def original_method(self, prompt):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = None

        wrapped = wrap_agent_run(original_method, mock_tracer, "run")
        result = await wrapped(mock_agent, "Hello")

        assert result is mock_result
        # Span should have been set with usage attributes
        assert mock_span.set_attribute.called

    def test_sync_wrapper_records_error(self):
        """Test sync wrapper records errors properly."""
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
            raise ValueError("Test error message")

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"

        wrapped = wrap_agent_run(original_method, mock_tracer, "run_sync")

        with pytest.raises(ValueError) as exc_info:
            wrapped(mock_agent, "Hello")

        assert str(exc_info.value) == "Test error message"
        mock_span.record_exception.assert_called_once()


class TestWrapToolFunctionExtended:
    """Extended tests for wrap_tool_function."""

    def test_tool_with_multiple_args(self):
        """Test wrapping tool with multiple arguments."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        def original_tool(ctx, arg1, arg2, arg3):
            return f"{arg1}-{arg2}-{arg3}"

        wrapped = wrap_tool_function(original_tool, mock_tracer, "multi_arg_tool")
        result = wrapped(MagicMock(), "a", "b", "c")

        assert result == "a-b-c"

    def test_tool_with_kwargs(self):
        """Test wrapping tool with keyword arguments."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        def original_tool(ctx, *, name, age):
            return f"{name} is {age}"

        wrapped = wrap_tool_function(original_tool, mock_tracer, "kwargs_tool")
        result = wrapped(MagicMock(), name="Alice", age=30)

        assert result == "Alice is 30"

    def test_tool_error_handling(self):
        """Test tool error is recorded properly."""
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
            raise RuntimeError("Tool failed")

        wrapped = wrap_tool_function(original_tool, mock_tracer, "failing_tool")

        with pytest.raises(RuntimeError):
            wrapped(MagicMock())

        mock_span.record_exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_tool_error_handling(self):
        """Test async tool error is recorded properly."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=None
        )

        async def original_tool(ctx):
            raise RuntimeError("Async tool failed")

        wrapped = wrap_tool_function(original_tool, mock_tracer, "async_failing_tool")

        with pytest.raises(RuntimeError):
            await wrapped(MagicMock())

        mock_span.record_exception.assert_called_once()

    def test_tool_preserves_function_metadata(self):
        """Test that wrapped tool preserves function metadata."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        mock_tracer = MagicMock()

        def original_tool(ctx, query: str) -> str:
            """Search for information."""
            return f"Result for {query}"

        wrapped = wrap_tool_function(
            original_tool,
            mock_tracer,
            "search_tool",
            tool_description="Search for information",
        )

        assert wrapped.__name__ == "original_tool"
        assert "Search" in (wrapped.__doc__ or "")


class TestSpanNameConstants:
    """Test span name constant values."""

    def test_span_names_are_unique(self):
        """Test that span names are unique."""
        from traceai_pydantic_ai._agent_wrapper import (
            AGENT_RUN_SPAN_NAME,
            MODEL_REQUEST_SPAN_NAME,
            TOOL_CALL_SPAN_NAME,
        )

        names = [AGENT_RUN_SPAN_NAME, MODEL_REQUEST_SPAN_NAME, TOOL_CALL_SPAN_NAME]
        assert len(names) == len(set(names))

    def test_span_names_follow_convention(self):
        """Test that span names follow naming convention."""
        from traceai_pydantic_ai._agent_wrapper import (
            AGENT_RUN_SPAN_NAME,
            MODEL_REQUEST_SPAN_NAME,
            TOOL_CALL_SPAN_NAME,
        )

        for name in [AGENT_RUN_SPAN_NAME, MODEL_REQUEST_SPAN_NAME, TOOL_CALL_SPAN_NAME]:
            assert name.startswith("pydantic_ai.")
            assert "." in name
