"""Tests for AutoGen v0.4 wrapper functions."""

import asyncio
import json
import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch

from traceai_autogen._v04_wrapper import (
    safe_serialize,
    extract_agent_info,
    extract_team_info,
    extract_message_info,
    extract_usage_from_result,
    wrap_agent_on_messages,
    wrap_team_run,
    wrap_tool_execution,
    TracedTeamRunStream,
)


class TestSafeSerialize:
    """Tests for safe_serialize function."""

    def test_serialize_none(self):
        """Test serializing None."""
        result = safe_serialize(None)
        assert result == ""

    def test_serialize_string(self):
        """Test serializing string."""
        result = safe_serialize("hello world")
        assert result == "hello world"

    def test_serialize_dict(self):
        """Test serializing dictionary."""
        result = safe_serialize({"key": "value"})
        assert result == '{"key": "value"}'

    def test_serialize_list(self):
        """Test serializing list."""
        result = safe_serialize([1, 2, 3])
        assert result == "[1, 2, 3]"

    def test_serialize_truncates_long_strings(self):
        """Test that long strings are truncated."""
        long_string = "a" * 3000
        result = safe_serialize(long_string, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_serialize_object(self):
        """Test serializing arbitrary object."""
        obj = MagicMock()
        obj.__str__ = MagicMock(return_value="MockObject")
        result = safe_serialize(obj)
        assert "MockObject" in result or "MagicMock" in result


class TestExtractAgentInfo:
    """Tests for extract_agent_info function."""

    def test_extract_basic_info(self):
        """Test extracting basic agent info."""
        agent = MagicMock()
        agent.name = "test_agent"
        type(agent).__name__ = "AssistantAgent"
        agent.description = "A test agent"
        agent._model_client = None
        agent._tools = []
        agent._memory = None

        info = extract_agent_info(agent)

        assert info["agent_name"] == "test_agent"
        assert info["agent_type"] == "AssistantAgent"
        assert info.get("agent_description") == "A test agent"

    def test_extract_with_model_client(self):
        """Test extracting info with model client."""
        agent = MagicMock()
        agent.name = "assistant"
        type(agent).__name__ = "AssistantAgent"
        agent.description = "Helper"
        agent._model_client = MagicMock()
        agent._model_client.model = "gpt-4"
        agent._tools = [MagicMock(), MagicMock()]
        agent._memory = True

        info = extract_agent_info(agent)

        assert info["agent_name"] == "assistant"
        assert info["model_name"] == "gpt-4"
        assert info["tool_count"] == 2
        assert info["has_memory"] is True

    def test_extract_with_private_model(self):
        """Test extracting model from _model attribute."""
        agent = MagicMock()
        agent.name = "agent"
        type(agent).__name__ = "Agent"
        del agent.description
        agent._model_client = MagicMock(spec=[])
        agent._model_client._model = "claude-3"
        del agent._tools
        del agent._memory

        info = extract_agent_info(agent)

        assert info["model_name"] == "claude-3"


class TestExtractTeamInfo:
    """Tests for extract_team_info function."""

    def test_extract_basic_team_info(self):
        """Test extracting basic team info."""
        team = MagicMock()
        type(team).__name__ = "RoundRobinGroupChat"
        team._participants = []
        team._termination_condition = None
        team._max_turns = None

        info = extract_team_info(team)

        assert info["team_type"] == "RoundRobinGroupChat"

    def test_extract_team_with_participants(self):
        """Test extracting team with participants."""
        agent1 = MagicMock()
        agent1.name = "agent1"
        agent2 = MagicMock()
        agent2.name = "agent2"

        team = MagicMock()
        type(team).__name__ = "SelectorGroupChat"
        team._participants = [agent1, agent2]
        team._termination_condition = MagicMock()
        type(team._termination_condition).__name__ = "MaxMessageTermination"
        team._max_turns = 10

        info = extract_team_info(team)

        assert info["team_type"] == "SelectorGroupChat"
        assert info["participant_count"] == 2
        assert info["participants"] == ["agent1", "agent2"]
        assert info["termination_condition"] == "MaxMessageTermination"
        assert info["max_turns"] == 10


class TestExtractMessageInfo:
    """Tests for extract_message_info function."""

    def test_extract_text_message(self):
        """Test extracting text message info."""
        message = MagicMock()
        type(message).__name__ = "TextMessage"
        message.content = "Hello, world!"
        message.source = "user"
        message.models_usage = None

        info = extract_message_info(message)

        assert info["message_type"] == "TextMessage"
        assert info["content"] == "Hello, world!"
        assert info["source"] == "user"

    def test_extract_message_with_usage(self):
        """Test extracting message with token usage."""
        message = MagicMock()
        type(message).__name__ = "AssistantMessage"
        message.content = "Response text"
        message.source = "assistant"
        message.models_usage = {"gpt-4": {"prompt_tokens": 100, "completion_tokens": 50}}

        info = extract_message_info(message)

        assert info["message_type"] == "AssistantMessage"
        assert "models_usage" in info


class TestExtractUsageFromResult:
    """Tests for extract_usage_from_result function."""

    def test_extract_no_result(self):
        """Test with no result."""
        assert extract_usage_from_result(None) is None

    def test_extract_no_messages(self):
        """Test with result but no messages."""
        result = MagicMock()
        result.messages = None
        assert extract_usage_from_result(result) is None

    def test_extract_empty_messages(self):
        """Test with empty messages list."""
        result = MagicMock()
        result.messages = []
        assert extract_usage_from_result(result) is None

    def test_extract_usage_from_messages(self):
        """Test extracting usage from messages."""
        usage1 = MagicMock()
        usage1.prompt_tokens = 100
        usage1.completion_tokens = 50

        msg1 = MagicMock()
        msg1.models_usage = usage1

        msg2 = MagicMock()
        msg2.models_usage = None

        result = MagicMock()
        result.messages = [msg1, msg2]

        usage = extract_usage_from_result(result)

        assert usage is not None
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["total_tokens"] == 150


class TestWrapAgentOnMessages:
    """Tests for wrap_agent_on_messages function."""

    def test_wrap_on_messages_success(self):
        """Test wrapping on_messages with successful execution."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def original_method(self, messages, cancellation_token=None):
            return MagicMock(chat_message=MagicMock(content="response"))

        wrapped = wrap_agent_on_messages(original_method, tracer)

        agent = MagicMock()
        agent.name = "test_agent"
        type(agent).__name__ = "TestAgent"
        agent._model_client = None
        agent._tools = []

        messages = [MagicMock(content="hello", source="user")]

        async def run_test():
            result = await wrapped(agent, messages)
            return result

        result = asyncio.run(run_test())

        assert result is not None
        tracer.start_as_current_span.assert_called_once()

    def test_wrap_on_messages_error(self):
        """Test wrapping on_messages with error."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def original_method(self, messages, cancellation_token=None):
            raise ValueError("Test error")

        wrapped = wrap_agent_on_messages(original_method, tracer)

        agent = MagicMock()
        agent.name = "error_agent"
        type(agent).__name__ = "Agent"
        agent._model_client = None
        agent._tools = []

        async def run_test():
            await wrapped(agent, [])

        with pytest.raises(ValueError, match="Test error"):
            asyncio.run(run_test())


class TestWrapTeamRun:
    """Tests for wrap_team_run function."""

    def test_wrap_team_run_success(self):
        """Test wrapping team run with success."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def original_run(self, task, *args, **kwargs):
            result = MagicMock()
            result.messages = [MagicMock()]
            result.stop_reason = "max_turns"
            return result

        wrapped = wrap_team_run(original_run, tracer, "run")

        team = MagicMock()
        type(team).__name__ = "RoundRobinGroupChat"
        team._participants = []
        team._termination_condition = None
        team._max_turns = None

        async def run_test():
            result = await wrapped(team, "Do something")
            return result

        result = asyncio.run(run_test())

        assert result is not None
        tracer.start_as_current_span.assert_called_once()

    def test_wrap_team_run_stream(self):
        """Test wrapping team run_stream."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def original_run_stream(self, task, *args, **kwargs):
            return MagicMock()

        wrapped = wrap_team_run(original_run_stream, tracer, "run_stream")

        team = MagicMock()
        type(team).__name__ = "SelectorGroupChat"
        team._participants = []
        team._termination_condition = None
        team._max_turns = None

        async def run_test():
            result = await wrapped(team, "Stream task")
            return result

        result = asyncio.run(run_test())
        assert result is not None


class TestWrapToolExecution:
    """Tests for wrap_tool_execution function."""

    def test_wrap_sync_tool(self):
        """Test wrapping synchronous tool."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def my_tool(x: int, y: int) -> int:
            return x + y

        wrapped = wrap_tool_execution(my_tool, tracer, "my_tool", "Add two numbers")

        result = wrapped(1, 2)
        assert result == 3
        tracer.start_as_current_span.assert_called_once()

    def test_wrap_async_tool(self):
        """Test wrapping asynchronous tool."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def my_async_tool(query: str) -> str:
            return f"Result for: {query}"

        wrapped = wrap_tool_execution(my_async_tool, tracer, "search", "Search for info")

        async def run_test():
            result = await wrapped("test query")
            return result

        result = asyncio.run(run_test())
        assert result == "Result for: test query"

    def test_wrap_tool_with_error(self):
        """Test wrapping tool that raises error."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def failing_tool():
            raise RuntimeError("Tool failed")

        wrapped = wrap_tool_execution(failing_tool, tracer, "failing_tool")

        with pytest.raises(RuntimeError, match="Tool failed"):
            wrapped()


class TestTracedTeamRunStream:
    """Tests for TracedTeamRunStream class."""

    def test_stream_iteration(self):
        """Test iterating over traced stream."""

        # Create mock stream
        class MockStream:
            def __init__(self):
                self._items = [
                    MagicMock(content="chunk1"),
                    MagicMock(content="chunk2"),
                    MagicMock(content="chunk3"),
                ]
                self._index = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._index >= len(self._items):
                    raise StopAsyncIteration
                item = self._items[self._index]
                self._index += 1
                return item

        mock_stream = MockStream()
        span = MagicMock()
        tracer = MagicMock()

        traced_stream = TracedTeamRunStream(
            mock_stream,
            span,
            time.time(),
            tracer
        )

        async def run_test():
            items = []
            async for item in traced_stream:
                items.append(item)
            return items

        items = asyncio.run(run_test())

        assert len(items) == 3
        assert traced_stream._message_count == 3

    def test_stream_context_manager(self):
        """Test stream as context manager."""

        class MockStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        mock_stream = MockStream()
        span = MagicMock()
        span.end = MagicMock()
        tracer = MagicMock()

        traced_stream = TracedTeamRunStream(
            mock_stream,
            span,
            time.time(),
            tracer
        )

        async def run_test():
            async with traced_stream as stream:
                async for _ in stream:
                    pass

        asyncio.run(run_test())

        # Verify span was ended
        span.end.assert_called_once()
