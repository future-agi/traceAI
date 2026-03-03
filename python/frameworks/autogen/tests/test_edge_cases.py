"""Edge case tests for AutoGen instrumentation.

These tests verify correct behavior in boundary conditions and unusual situations.
"""

import asyncio
import json
import pytest
from unittest.mock import MagicMock

from traceai_autogen._v04_wrapper import (
    safe_serialize,
    extract_agent_info,
    extract_team_info,
    extract_message_info,
    extract_usage_from_result,
    wrap_agent_on_messages,
    wrap_team_run,
    wrap_tool_execution,
)
from traceai_autogen._attributes import get_model_provider, get_agent_type_name


class TestSafeSerializeEdgeCases:
    """Edge cases for safe_serialize function."""

    def test_serialize_empty_string(self):
        """Test serializing empty string."""
        result = safe_serialize("")
        assert result == ""

    def test_serialize_empty_dict(self):
        """Test serializing empty dict."""
        result = safe_serialize({})
        assert result == "{}"

    def test_serialize_empty_list(self):
        """Test serializing empty list."""
        result = safe_serialize([])
        assert result == "[]"

    def test_serialize_nested_structure(self):
        """Test serializing deeply nested structure."""
        nested = {"level1": {"level2": {"level3": {"data": [1, 2, 3]}}}}
        result = safe_serialize(nested)
        assert "level1" in result
        assert "level3" in result

    def test_serialize_unicode(self):
        """Test serializing unicode characters."""
        result = safe_serialize("Hello 世界 🌍")
        assert "世界" in result

    def test_serialize_special_characters(self):
        """Test serializing special characters."""
        result = safe_serialize("Line1\nLine2\tTabbed")
        assert "\\n" in result or "\n" in result

    def test_serialize_circular_reference_fallback(self):
        """Test serializing object that can't be JSON serialized."""
        obj = MagicMock()
        result = safe_serialize(obj)
        assert isinstance(result, str)

    def test_serialize_bytes(self):
        """Test serializing bytes object."""
        result = safe_serialize(b"hello bytes")
        assert isinstance(result, str)

    def test_serialize_at_max_length(self):
        """Test serializing string exactly at max length."""
        exact_string = "a" * 100
        result = safe_serialize(exact_string, max_length=100)
        assert len(result) == 100
        assert not result.endswith("...")

    def test_serialize_just_over_max_length(self):
        """Test serializing string just over max length."""
        over_string = "a" * 101
        result = safe_serialize(over_string, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")


class TestExtractAgentInfoEdgeCases:
    """Edge cases for extract_agent_info function."""

    def test_agent_with_no_attributes(self):
        """Test agent with minimal attributes."""
        agent = MagicMock(spec=[])
        agent.name = "minimal_agent"
        type(agent).__name__ = "Agent"

        info = extract_agent_info(agent)

        assert info["agent_name"] == "minimal_agent"
        assert info["agent_type"] == "Agent"

    def test_agent_with_empty_name(self):
        """Test agent with empty name."""
        agent = MagicMock()
        agent.name = ""
        type(agent).__name__ = "Agent"

        info = extract_agent_info(agent)

        assert info["agent_name"] == ""

    def test_agent_with_none_model_client(self):
        """Test agent with None model client."""
        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        agent._model_client = None

        info = extract_agent_info(agent)

        assert "model_name" not in info

    def test_agent_with_empty_tools_list(self):
        """Test agent with empty tools list."""
        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        agent._tools = []

        info = extract_agent_info(agent)

        assert info.get("tool_count", 0) == 0

    def test_agent_with_many_tools(self):
        """Test agent with many tools."""
        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        agent._tools = [MagicMock() for _ in range(100)]

        info = extract_agent_info(agent)

        assert info["tool_count"] == 100

    def test_agent_with_very_long_description(self):
        """Test agent with very long description."""
        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        agent.description = "x" * 1000

        info = extract_agent_info(agent)

        # Should be truncated
        assert len(info["agent_description"]) <= 500


class TestExtractTeamInfoEdgeCases:
    """Edge cases for extract_team_info function."""

    def test_team_with_no_participants(self):
        """Test team with no participants."""
        team = MagicMock()
        type(team).__name__ = "EmptyTeam"
        team._participants = []

        info = extract_team_info(team)

        assert info["team_type"] == "EmptyTeam"
        assert "participant_count" not in info

    def test_team_with_single_participant(self):
        """Test team with single participant."""
        agent = MagicMock()
        agent.name = "solo_agent"

        team = MagicMock()
        type(team).__name__ = "Team"
        team._participants = [agent]

        info = extract_team_info(team)

        assert info["participant_count"] == 1
        assert info["participants"] == ["solo_agent"]

    def test_team_with_many_participants(self):
        """Test team with many participants."""
        agents = [MagicMock(name=f"agent_{i}") for i in range(50)]
        for i, a in enumerate(agents):
            a.name = f"agent_{i}"

        team = MagicMock()
        type(team).__name__ = "LargeTeam"
        team._participants = agents

        info = extract_team_info(team)

        assert info["participant_count"] == 50

    def test_team_with_none_termination(self):
        """Test team with None termination condition."""
        team = MagicMock()
        type(team).__name__ = "Team"
        team._participants = []
        team._termination_condition = None

        info = extract_team_info(team)

        assert "termination_condition" not in info


class TestExtractMessageInfoEdgeCases:
    """Edge cases for extract_message_info function."""

    def test_message_with_empty_content(self):
        """Test message with empty content."""
        message = MagicMock()
        type(message).__name__ = "Message"
        message.content = ""
        message.source = "user"

        info = extract_message_info(message)

        assert info["content"] == ""

    def test_message_with_none_source(self):
        """Test message with None source."""
        message = MagicMock()
        type(message).__name__ = "Message"
        message.content = "test"
        message.source = None

        info = extract_message_info(message)

        assert info["source"] == "None"

    def test_message_with_complex_content(self):
        """Test message with complex content structure."""
        message = MagicMock()
        type(message).__name__ = "Message"
        message.content = {"text": "hello", "images": ["img1.png", "img2.png"]}
        message.source = "user"

        info = extract_message_info(message)

        assert "text" in info["content"] or "hello" in info["content"]


class TestExtractUsageEdgeCases:
    """Edge cases for extract_usage_from_result function."""

    def test_result_with_empty_usage(self):
        """Test result with messages but no usage."""
        msg = MagicMock()
        msg.models_usage = None

        result = MagicMock()
        result.messages = [msg]

        usage = extract_usage_from_result(result)

        assert usage is None

    def test_result_with_zero_tokens(self):
        """Test result with zero token counts."""
        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 0
        usage_obj.completion_tokens = 0

        msg = MagicMock()
        msg.models_usage = usage_obj

        result = MagicMock()
        result.messages = [msg]

        usage = extract_usage_from_result(result)

        # Zero usage should still return None
        assert usage is None

    def test_result_with_dict_usage(self):
        """Test result with dict-style usage."""
        model_usage = MagicMock()
        model_usage.prompt_tokens = 100
        model_usage.completion_tokens = 50

        msg = MagicMock()
        msg.models_usage = {"gpt-4": model_usage}

        result = MagicMock()
        result.messages = [msg]

        usage = extract_usage_from_result(result)

        assert usage is not None
        assert usage["input_tokens"] == 100


class TestWrapperEdgeCases:
    """Edge cases for wrapper functions."""

    def test_wrap_agent_with_empty_messages(self):
        """Test wrapping agent called with empty message list."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def original(self, messages, cancellation_token=None):
            return MagicMock(chat_message=MagicMock(content="response"))

        wrapped = wrap_agent_on_messages(original, tracer)

        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        agent._model_client = None
        agent._tools = []

        async def run_test():
            return await wrapped(agent, [])

        result = asyncio.run(run_test())
        assert result is not None

    def test_wrap_team_with_none_task(self):
        """Test wrapping team run with None task."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def original(self, task, *args, **kwargs):
            result = MagicMock()
            result.messages = []
            result.stop_reason = "complete"
            return result

        wrapped = wrap_team_run(original, tracer, "run")

        team = MagicMock()
        type(team).__name__ = "Team"
        team._participants = []
        team._termination_condition = None
        team._max_turns = None

        async def run_test():
            return await wrapped(team, None)

        result = asyncio.run(run_test())
        assert result is not None

    def test_wrap_tool_with_no_args(self):
        """Test wrapping tool called with no arguments."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def get_time() -> str:
            return "12:00"

        wrapped = wrap_tool_execution(get_time, tracer, "get_time")

        result = wrapped()
        assert result == "12:00"

    def test_wrap_tool_with_kwargs_only(self):
        """Test wrapping tool called with kwargs only."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def greet(name: str = "World") -> str:
            return f"Hello, {name}!"

        wrapped = wrap_tool_execution(greet, tracer, "greet")

        result = wrapped(name="Alice")
        assert result == "Hello, Alice!"


class TestModelProviderEdgeCases:
    """Edge cases for model provider detection."""

    def test_empty_model_name(self):
        """Test empty model name."""
        assert get_model_provider("") == "unknown"

    def test_none_model_name(self):
        """Test None model name."""
        assert get_model_provider(None) == "unknown"

    def test_whitespace_model_name(self):
        """Test whitespace-only model name."""
        assert get_model_provider("   ") == "unknown"

    def test_model_with_version_suffix(self):
        """Test model name with version suffix."""
        assert get_model_provider("gpt-4-0125-preview") == "openai"
        assert get_model_provider("claude-3-sonnet-20240229") == "anthropic"

    def test_model_with_prefix_and_slash(self):
        """Test model with both prefix and slash."""
        assert get_model_provider("azure/gpt-4") == "azure"

    def test_case_sensitivity(self):
        """Test case sensitivity of model detection."""
        assert get_model_provider("GPT-4") == "openai"
        assert get_model_provider("CLAUDE-3") == "anthropic"
        assert get_model_provider("Gemini-Pro") == "google"

    def test_custom_model_name(self):
        """Test completely custom model name."""
        assert get_model_provider("my-custom-model-v2") == "unknown"


class TestAgentTypeNameEdgeCases:
    """Edge cases for get_agent_type_name function."""

    def test_builtin_type(self):
        """Test with built-in type."""
        assert get_agent_type_name("string") == "str"
        assert get_agent_type_name(123) == "int"
        assert get_agent_type_name([1, 2, 3]) == "list"

    def test_custom_class(self):
        """Test with custom class."""
        class MyCustomAgent:
            pass

        agent = MyCustomAgent()
        assert get_agent_type_name(agent) == "MyCustomAgent"

    def test_nested_class(self):
        """Test with nested class."""
        class Outer:
            class Inner:
                pass

        obj = Outer.Inner()
        assert get_agent_type_name(obj) == "Inner"


class TestConcurrentOperations:
    """Test concurrent/parallel operations."""

    def test_concurrent_tool_calls(self):
        """Test multiple concurrent tool calls."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def async_tool(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        wrapped = wrap_tool_execution(async_tool, tracer, "async_tool")

        async def run_test():
            tasks = [wrapped(i) for i in range(5)]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(run_test())

        assert results == [0, 2, 4, 6, 8]
        assert tracer.start_as_current_span.call_count == 5

    def test_concurrent_agent_calls(self):
        """Test multiple concurrent agent calls."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def agent_method(self, messages, cancellation_token=None):
            await asyncio.sleep(0.01)
            return MagicMock(chat_message=MagicMock(content=f"Response to: {messages[0].content}"))

        wrapped = wrap_agent_on_messages(agent_method, tracer)

        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        agent._model_client = None
        agent._tools = []

        async def run_test():
            tasks = []
            for i in range(3):
                msg = MagicMock(content=f"Question {i}", source="user")
                tasks.append(wrapped(agent, [msg]))
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(run_test())

        assert len(results) == 3
        assert tracer.start_as_current_span.call_count == 3
