"""Tests for Claude Agent SDK client wrapper."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import time


class TestTurnTracker:
    """Test TurnTracker class."""

    def test_init_state(self):
        """Test TurnTracker initial state."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        assert tracker.current_turn_span is None
        assert tracker.turn_count == 0
        assert tracker.parent_span is mock_parent

    def test_init_with_start_time(self):
        """Test TurnTracker with query start time."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_parent = MagicMock()
        start_time = time.time()

        tracker = TurnTracker(mock_tracer, mock_parent, query_start_time=start_time)

        assert tracker.next_start_time == start_time

    def test_start_turn_creates_span(self):
        """Test that start_turn creates a span."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        # Create mock message
        mock_message = MagicMock()
        mock_message.model = "claude-3-opus"
        mock_message.content = "Hello"

        tracker.start_turn(mock_message)

        assert tracker.current_turn_span is mock_span
        assert tracker.turn_count == 1
        mock_tracer.start_span.assert_called()

    def test_start_turn_ends_previous(self):
        """Test that start_turn ends previous turn."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_span1 = MagicMock()
        mock_span2 = MagicMock()
        mock_tracer.start_span.side_effect = [mock_span1, mock_span2]
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        mock_message = MagicMock()
        mock_message.model = None
        mock_message.content = None

        tracker.start_turn(mock_message)
        tracker.start_turn(mock_message)

        mock_span1.end.assert_called_once()
        assert tracker.current_turn_span is mock_span2
        assert tracker.turn_count == 2

    def test_end_current_turn_ends_span(self):
        """Test that end_current_turn ends the span."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        mock_message = MagicMock()
        mock_message.model = None
        mock_message.content = None

        tracker.start_turn(mock_message)
        tracker.end_current_turn()

        mock_span.end.assert_called_once()
        assert tracker.current_turn_span is None

    def test_end_current_turn_with_usage(self):
        """Test end_current_turn with usage metrics."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        mock_message = MagicMock()
        mock_message.model = None
        mock_message.content = None

        tracker.start_turn(mock_message)
        tracker.end_current_turn(usage={
            "input_tokens": 100,
            "output_tokens": 50,
        })

        # Verify set_attribute was called with usage
        calls = mock_span.set_attribute.call_args_list
        attr_names = [c[0][0] for c in calls]
        assert any("input_tokens" in str(n) for n in attr_names)

    def test_end_current_turn_safe_without_span(self):
        """Test that end_current_turn is safe without active span."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        # Should not raise
        tracker.end_current_turn()

    def test_mark_next_start(self):
        """Test mark_next_start sets timestamp."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        before = time.time()
        tracker.mark_next_start()
        after = time.time()

        assert before <= tracker.next_start_time <= after

    def test_flatten_content_string(self):
        """Test _flatten_content with string."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        result = tracker._flatten_content("Hello world")
        assert result == "Hello world"

    def test_flatten_content_list_with_text_blocks(self):
        """Test _flatten_content with text blocks."""
        from traceai_claude_agent_sdk._client_wrapper import TurnTracker

        mock_tracer = MagicMock()
        mock_parent = MagicMock()

        tracker = TurnTracker(mock_tracer, mock_parent)

        block1 = MagicMock()
        block1.text = "Hello"
        block2 = MagicMock()
        block2.text = "World"

        result = tracker._flatten_content([block1, block2])
        assert "Hello" in result
        assert "World" in result


class TestInjectTracingHooks:
    """Test inject_tracing_hooks function."""

    def test_does_nothing_without_hooks_attr(self):
        """Test that function is safe without hooks attribute."""
        from traceai_claude_agent_sdk._client_wrapper import inject_tracing_hooks

        mock_tracer = MagicMock()
        options = MagicMock(spec=[])  # No hooks attribute

        # Should not raise
        inject_tracing_hooks(options, mock_tracer)

    def test_initializes_hooks_if_none(self):
        """Test that hooks dict is initialized if None."""
        from traceai_claude_agent_sdk._client_wrapper import inject_tracing_hooks

        mock_tracer = MagicMock()
        options = MagicMock()
        options.hooks = None

        inject_tracing_hooks(options, mock_tracer)

        assert options.hooks == {} or isinstance(options.hooks, dict)

    @patch("traceai_claude_agent_sdk._client_wrapper.create_pre_tool_use_hook")
    @patch("traceai_claude_agent_sdk._client_wrapper.create_post_tool_use_hook")
    def test_creates_hook_lists(self, mock_post, mock_pre):
        """Test that PreToolUse and PostToolUse lists are created."""
        from traceai_claude_agent_sdk._client_wrapper import inject_tracing_hooks

        mock_tracer = MagicMock()
        options = MagicMock()
        options.hooks = {}

        # Mock HookMatcher import
        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            inject_tracing_hooks(options, mock_tracer)

        # Hooks should be initialized
        assert "PreToolUse" in options.hooks or options.hooks.get("PreToolUse") is not None


class TestWrapClaudeSdkClient:
    """Test wrap_claude_sdk_client function."""

    def test_returns_class(self):
        """Test that wrap returns a class."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)

        assert isinstance(wrapped, type)

    def test_wrapped_class_is_subclass(self):
        """Test that wrapped class is a subclass."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)

        assert issubclass(wrapped, MockClaudeSDKClient)

    def test_wrapped_class_name_contains_traced(self):
        """Test wrapped class has appropriate name."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)

        assert "Traced" in wrapped.__name__

    def test_wrapped_class_has_query_method(self):
        """Test that wrapped class has query method."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            async def query(self, prompt, options=None):
                pass

            async def receive_response(self):
                yield {"type": "result"}

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)

        assert hasattr(wrapped, "query")

    def test_wrapped_class_has_receive_response_method(self):
        """Test that wrapped class has receive_response method."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            async def receive_response(self):
                yield {"type": "result"}

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)

        assert hasattr(wrapped, "receive_response")


class TestTracedClientInit:
    """Test TracedClaudeSDKClient initialization."""

    def test_stores_tracer(self):
        """Test that tracer is stored on instance."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self, options=None):
                self.options = options

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        assert instance._tracer is mock_tracer

    def test_injects_hooks_on_init(self):
        """Test that hooks are injected on initialization."""
        from traceai_claude_agent_sdk._client_wrapper import (
            wrap_claude_sdk_client,
            inject_tracing_hooks,
        )

        mock_tracer = MagicMock()
        mock_options = MagicMock()
        mock_options.hooks = {}
        mock_options.mcp_servers = None

        class MockClaudeSDKClient:
            def __init__(self, options=None):
                self.options = options

        with patch(
            "traceai_claude_agent_sdk._client_wrapper.inject_tracing_hooks"
        ) as mock_inject:
            wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
            instance = wrapped(options=mock_options)

            mock_inject.assert_called_once_with(mock_options, mock_tracer)


class TestSpanNames:
    """Test span name constants."""

    def test_conversation_span_name(self):
        """Test conversation span name constant."""
        from traceai_claude_agent_sdk._client_wrapper import CONVERSATION_SPAN_NAME

        assert CONVERSATION_SPAN_NAME == "claude_agent.conversation"

    def test_assistant_turn_span_name(self):
        """Test assistant turn span name constant."""
        from traceai_claude_agent_sdk._client_wrapper import ASSISTANT_TURN_SPAN_NAME

        assert ASSISTANT_TURN_SPAN_NAME == "claude_agent.assistant_turn"


class TestContentExtraction:
    """Test content extraction from messages."""

    def test_extract_string_content(self):
        """Test extracting string content."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        mock_msg = MagicMock()
        mock_msg.content = "Hello world"

        result = instance._extract_content(mock_msg)
        assert result == "Hello world"

    def test_extract_none_content(self):
        """Test extracting None content."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        mock_msg = MagicMock()
        mock_msg.content = None

        result = instance._extract_content(mock_msg)
        assert result is None

    def test_extract_list_content_with_text_blocks(self):
        """Test extracting list content with text blocks."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        block1 = MagicMock()
        block1.text = "Hello"
        block2 = MagicMock()
        block2.text = "World"

        mock_msg = MagicMock()
        mock_msg.content = [block1, block2]

        result = instance._extract_content(mock_msg)
        assert "Hello" in result
        assert "World" in result


class TestUsageExtraction:
    """Test usage extraction from ResultMessage."""

    def test_extract_usage_with_metrics(self):
        """Test extracting usage metrics."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_read_input_tokens = 10
        mock_usage.cache_creation_input_tokens = 5

        mock_msg = MagicMock()
        mock_msg.usage = mock_usage

        result = instance._extract_usage(mock_msg)

        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["cache_read_input_tokens"] == 10
        assert result["cache_creation_input_tokens"] == 5

    def test_extract_usage_none(self):
        """Test extracting usage when none."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        mock_msg = MagicMock()
        mock_msg.usage = None

        result = instance._extract_usage(mock_msg)
        assert result is None


class TestBuildSpanAttributes:
    """Test _build_span_attributes method."""

    def test_includes_span_kind(self):
        """Test that span kind is included."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client
        from traceai_claude_agent_sdk._attributes import (
            ClaudeAgentAttributes,
            ClaudeAgentSpanKind,
        )

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()
        instance._options = None
        instance._prompt = None

        attrs = instance._build_span_attributes()

        assert ClaudeAgentAttributes.SPAN_KIND in attrs
        assert attrs[ClaudeAgentAttributes.SPAN_KIND] == ClaudeAgentSpanKind.CONVERSATION.value

    def test_includes_prompt(self):
        """Test that prompt is included."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()
        instance._options = None
        instance._prompt = "What is the weather?"

        attrs = instance._build_span_attributes()

        assert ClaudeAgentAttributes.AGENT_PROMPT in attrs
        assert attrs[ClaudeAgentAttributes.AGENT_PROMPT] == "What is the weather?"

    def test_includes_model_from_options(self):
        """Test that model is included from options."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()

        mock_options = MagicMock()
        mock_options.model = "claude-3-opus"
        mock_options.permission_mode = None
        mock_options.allowed_tools = None
        mock_options.system_prompt = None
        mock_options.resume = None

        instance._options = mock_options
        instance._prompt = None

        attrs = instance._build_span_attributes()

        assert ClaudeAgentAttributes.AGENT_MODEL in attrs
        assert attrs[ClaudeAgentAttributes.AGENT_MODEL] == "claude-3-opus"

    def test_truncates_long_prompt(self):
        """Test that long prompts are truncated."""
        from traceai_claude_agent_sdk._client_wrapper import wrap_claude_sdk_client
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        mock_tracer = MagicMock()

        class MockClaudeSDKClient:
            def __init__(self):
                pass

        wrapped = wrap_claude_sdk_client(MockClaudeSDKClient, mock_tracer)
        instance = wrapped()
        instance._options = None
        instance._prompt = "x" * 5000

        attrs = instance._build_span_attributes()

        assert len(attrs[ClaudeAgentAttributes.AGENT_PROMPT]) <= 2000
