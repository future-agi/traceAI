"""Tests for Claude Agent SDK hooks."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestSafeJsonSerialize:
    """Test safe_json_serialize function."""

    def test_serialize_none(self):
        """Test serializing None."""
        from traceai_claude_agent_sdk._hooks import safe_json_serialize

        assert safe_json_serialize(None) == ""

    def test_serialize_string(self):
        """Test serializing string."""
        from traceai_claude_agent_sdk._hooks import safe_json_serialize

        assert safe_json_serialize("hello") == "hello"

    def test_serialize_dict(self):
        """Test serializing dict."""
        from traceai_claude_agent_sdk._hooks import safe_json_serialize

        result = safe_json_serialize({"key": "value"})
        assert result == '{"key": "value"}'

    def test_serialize_list(self):
        """Test serializing list."""
        from traceai_claude_agent_sdk._hooks import safe_json_serialize

        result = safe_json_serialize([1, 2, 3])
        assert result == "[1, 2, 3]"

    def test_serialize_truncates_long_string(self):
        """Test that long strings are truncated."""
        from traceai_claude_agent_sdk._hooks import safe_json_serialize

        long_string = "a" * 10000
        result = safe_json_serialize(long_string, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_serialize_complex_object(self):
        """Test serializing complex object."""
        from traceai_claude_agent_sdk._hooks import safe_json_serialize

        obj = {"nested": {"key": [1, 2, {"deep": "value"}]}}
        result = safe_json_serialize(obj)
        assert "nested" in result
        assert "deep" in result


class TestParentSpanManagement:
    """Test parent span management functions."""

    def test_set_and_get_parent_span(self):
        """Test setting and getting parent span."""
        from traceai_claude_agent_sdk._hooks import (
            set_parent_span,
            get_parent_span,
            clear_parent_span,
        )

        mock_span = MagicMock()
        set_parent_span(mock_span)
        assert get_parent_span() == mock_span

        clear_parent_span()
        assert get_parent_span() is None

    def test_clear_parent_span(self):
        """Test clearing parent span."""
        from traceai_claude_agent_sdk._hooks import (
            set_parent_span,
            get_parent_span,
            clear_parent_span,
        )

        mock_span = MagicMock()
        set_parent_span(mock_span)
        clear_parent_span()
        assert get_parent_span() is None


class TestMCPServerManagement:
    """Test MCP server management functions."""

    def test_set_mcp_servers(self):
        """Test setting MCP servers."""
        from traceai_claude_agent_sdk._hooks import set_mcp_servers, _mcp_servers

        servers = {"playwright": {"command": "npx"}}
        set_mcp_servers(servers)
        # Note: _mcp_servers is module-level, check it was set

    def test_set_mcp_servers_none(self):
        """Test setting MCP servers to None."""
        from traceai_claude_agent_sdk._hooks import set_mcp_servers

        set_mcp_servers(None)  # Should not raise


class TestSubagentSpanManagement:
    """Test subagent span management."""

    def test_get_subagent_span_not_found(self):
        """Test getting non-existent subagent span."""
        from traceai_claude_agent_sdk._hooks import get_subagent_span

        result = get_subagent_span("nonexistent-id")
        assert result is None


class TestCreatePreToolUseHook:
    """Test create_pre_tool_use_hook function."""

    def test_creates_callable(self):
        """Test that create_pre_tool_use_hook returns a callable."""
        from traceai_claude_agent_sdk._hooks import create_pre_tool_use_hook

        mock_tracer = MagicMock()
        hook = create_pre_tool_use_hook(mock_tracer)
        assert callable(hook)

    @pytest.mark.asyncio
    async def test_hook_returns_empty_dict_without_tool_use_id(self):
        """Test hook returns empty dict when tool_use_id is None."""
        from traceai_claude_agent_sdk._hooks import create_pre_tool_use_hook

        mock_tracer = MagicMock()
        hook = create_pre_tool_use_hook(mock_tracer)

        result = await hook(
            input_data={"tool_name": "Read"},
            tool_use_id=None,
            context=MagicMock(),
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_hook_creates_span_with_tool_use_id(self):
        """Test hook creates span when tool_use_id is provided."""
        from traceai_claude_agent_sdk._hooks import (
            create_pre_tool_use_hook,
            _active_tool_spans,
            set_parent_span,
            clear_parent_span,
        )

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        # Set up parent span
        parent_span = MagicMock()
        set_parent_span(parent_span)

        hook = create_pre_tool_use_hook(mock_tracer)

        try:
            result = await hook(
                input_data={"tool_name": "Read", "tool_input": {"file_path": "test.py"}},
                tool_use_id="test-id-123",
                context=MagicMock(),
            )

            assert result == {}
            assert "test-id-123" in _active_tool_spans

            # Cleanup
            _active_tool_spans.pop("test-id-123", None)
        finally:
            clear_parent_span()


class TestCreatePostToolUseHook:
    """Test create_post_tool_use_hook function."""

    def test_creates_callable(self):
        """Test that create_post_tool_use_hook returns a callable."""
        from traceai_claude_agent_sdk._hooks import create_post_tool_use_hook

        mock_tracer = MagicMock()
        hook = create_post_tool_use_hook(mock_tracer)
        assert callable(hook)

    @pytest.mark.asyncio
    async def test_hook_returns_empty_dict_without_tool_use_id(self):
        """Test hook returns empty dict when tool_use_id is None."""
        from traceai_claude_agent_sdk._hooks import create_post_tool_use_hook

        mock_tracer = MagicMock()
        hook = create_post_tool_use_hook(mock_tracer)

        result = await hook(
            input_data={"tool_name": "Read", "tool_response": "content"},
            tool_use_id=None,
            context=MagicMock(),
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_hook_ends_span_when_found(self):
        """Test hook ends span when matching PreToolUse found."""
        from traceai_claude_agent_sdk._hooks import (
            create_post_tool_use_hook,
            _active_tool_spans,
        )
        import time

        mock_tracer = MagicMock()
        mock_span = MagicMock()

        # Pre-populate active spans
        _active_tool_spans["test-id-456"] = (mock_span, time.time(), "Read")

        hook = create_post_tool_use_hook(mock_tracer)

        result = await hook(
            input_data={"tool_name": "Read", "tool_response": "file content"},
            tool_use_id="test-id-456",
            context=MagicMock(),
        )

        assert result == {}
        assert "test-id-456" not in _active_tool_spans
        mock_span.end.assert_called_once()


class TestClearActiveToolSpans:
    """Test clear_active_tool_spans function."""

    def test_clears_all_spans(self):
        """Test that all active spans are cleared."""
        from traceai_claude_agent_sdk._hooks import (
            clear_active_tool_spans,
            _active_tool_spans,
            _subagent_spans,
        )
        import time

        mock_span = MagicMock()
        _active_tool_spans["id1"] = (mock_span, time.time(), "Read")
        _subagent_spans["id2"] = mock_span

        clear_active_tool_spans()

        assert len(_active_tool_spans) == 0
        assert len(_subagent_spans) == 0
