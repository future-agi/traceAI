"""Tests for MCPTracker."""

import pytest
from unittest.mock import MagicMock


class TestMCPServerInfo:
    """Test MCPServerInfo dataclass."""

    def test_create_mcp_server_info(self):
        """Test creating MCPServerInfo."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPServerInfo

        info = MCPServerInfo(
            name="playwright",
            command="npx",
            args=["-y", "@anthropic/mcp-playwright"],
        )

        assert info.name == "playwright"
        assert info.command == "npx"
        assert info.args == ["-y", "@anthropic/mcp-playwright"]
        assert info.tool_calls == 0

    def test_default_values(self):
        """Test default values."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPServerInfo

        info = MCPServerInfo(name="test")

        assert info.command is None
        assert info.args is None
        assert info.url is None
        assert info.tools == set()


class TestMCPTracker:
    """Test MCPTracker class."""

    def test_init(self):
        """Test MCPTracker initialization."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()

        assert tracker.server_count == 0
        assert tracker.server_names == []

    def test_register_servers(self):
        """Test registering MCP servers."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()

        servers = {
            "playwright": {
                "command": "npx",
                "args": ["-y", "@anthropic/mcp-playwright"],
            },
            "puppeteer": {
                "command": "node",
                "args": ["puppeteer.js"],
            },
        }

        tracker.register_servers(servers)

        assert tracker.server_count == 2
        assert "playwright" in tracker.server_names
        assert "puppeteer" in tracker.server_names

    def test_register_servers_none(self):
        """Test registering None servers."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers(None)

        assert tracker.server_count == 0

    def test_register_servers_empty(self):
        """Test registering empty servers."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({})

        assert tracker.server_count == 0

    def test_register_tool(self):
        """Test registering a tool."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({"playwright": {"command": "npx"}})

        tracker.register_tool("mcp__playwright__click", "playwright")

        info = tracker.get_server_info("playwright")
        assert "mcp__playwright__click" in info.tools

    def test_get_tool_server(self):
        """Test getting server for a tool."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({"playwright": {"command": "npx"}})

        # Test explicit registration
        tracker.register_tool("mcp__playwright__click", "playwright")
        assert tracker.get_tool_server("mcp__playwright__click") == "playwright"

        # Test pattern matching
        assert tracker.get_tool_server("mcp__playwright__hover") == "playwright"

        # Test non-MCP tool
        assert tracker.get_tool_server("Read") is None

    def test_parse_tool_name(self):
        """Test parsing MCP tool names."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()

        result = tracker.parse_tool_name("mcp__playwright__click")
        assert result == {"server": "playwright", "tool": "click"}

        result = tracker.parse_tool_name("mcp__puppeteer__screenshot")
        assert result == {"server": "puppeteer", "tool": "screenshot"}

        result = tracker.parse_tool_name("Read")
        assert result is None

    def test_is_mcp_tool(self):
        """Test checking if tool is MCP tool."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({"playwright": {}})

        assert tracker.is_mcp_tool("mcp__playwright__click") is True
        assert tracker.is_mcp_tool("Read") is False
        assert tracker.is_mcp_tool("Bash") is False

    def test_annotate_tool_span(self):
        """Test annotating span with MCP info."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        tracker = MCPTracker()
        tracker.register_servers({
            "playwright": {
                "command": "npx",
                "args": ["-y", "@anthropic/mcp-playwright"],
            }
        })

        mock_span = MagicMock()

        result = tracker.annotate_tool_span(mock_span, "mcp__playwright__click")

        assert result is True
        mock_span.set_attribute.assert_called()

        calls = mock_span.set_attribute.call_args_list
        attr_dict = {c[0][0]: c[0][1] for c in calls}

        assert attr_dict[ClaudeAgentAttributes.MCP_SERVER_NAME] == "playwright"
        assert attr_dict[ClaudeAgentAttributes.MCP_SERVER_COMMAND] == "npx"

    def test_annotate_tool_span_non_mcp(self):
        """Test annotating non-MCP tool returns False."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        mock_span = MagicMock()

        result = tracker.annotate_tool_span(mock_span, "Read")

        assert result is False
        mock_span.set_attribute.assert_not_called()

    def test_annotate_tool_span_with_duration(self):
        """Test annotating with duration updates stats."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({"playwright": {"command": "npx"}})

        mock_span = MagicMock()

        tracker.annotate_tool_span(
            mock_span,
            "mcp__playwright__click",
            duration_ms=150.0,
        )

        info = tracker.get_server_info("playwright")
        assert info.tool_calls == 1
        assert info.total_duration_ms == 150.0

    def test_get_server_info(self):
        """Test getting server info."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({
            "playwright": {"command": "npx"}
        })

        info = tracker.get_server_info("playwright")
        assert info is not None
        assert info.name == "playwright"
        assert info.command == "npx"

        info = tracker.get_server_info("nonexistent")
        assert info is None

    def test_get_server_stats(self):
        """Test getting server stats."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({"playwright": {"command": "npx"}})

        mock_span = MagicMock()
        tracker.annotate_tool_span(mock_span, "mcp__playwright__click", 100.0)
        tracker.annotate_tool_span(mock_span, "mcp__playwright__hover", 50.0)

        stats = tracker.get_server_stats("playwright")

        assert stats["name"] == "playwright"
        assert stats["command"] == "npx"
        assert stats["total_calls"] == 2
        assert stats["total_duration_ms"] == 150.0

    def test_get_all_server_stats(self):
        """Test getting all server stats."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({
            "playwright": {"command": "npx"},
            "puppeteer": {"command": "node"},
        })

        all_stats = tracker.get_all_server_stats()

        assert "playwright" in all_stats
        assert "puppeteer" in all_stats

    def test_annotate_conversation_span(self):
        """Test annotating conversation span with MCP info."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({
            "playwright": {"command": "npx"},
            "puppeteer": {"command": "node"},
        })

        mock_span = MagicMock()
        tracker.annotate_conversation_span(mock_span)

        mock_span.set_attribute.assert_called()
        calls = mock_span.set_attribute.call_args_list
        attr_dict = {c[0][0]: c[0][1] for c in calls}

        assert "playwright" in attr_dict["claude_agent.mcp.servers"]
        assert "puppeteer" in attr_dict["claude_agent.mcp.servers"]
        assert attr_dict["claude_agent.mcp.server_count"] == 2

    def test_annotate_conversation_span_no_servers(self):
        """Test annotating when no servers configured."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        mock_span = MagicMock()

        tracker.annotate_conversation_span(mock_span)

        mock_span.set_attribute.assert_not_called()

    def test_clear(self):
        """Test clearing tracker."""
        from traceai_claude_agent_sdk._mcp_tracker import MCPTracker

        tracker = MCPTracker()
        tracker.register_servers({"playwright": {}})
        tracker.register_tool("mcp__playwright__click", "playwright")

        tracker.clear()

        assert tracker.server_count == 0
        # After clear, explicitly registered tools are removed
        # But pattern matching still works for MCP tool format
        # (returns server name parsed from tool name)
        assert tracker.get_server_info("playwright") is None


class TestMCPToolPattern:
    """Test MCP tool name pattern."""

    def test_mcp_tool_pattern(self):
        """Test the MCP tool pattern regex."""
        from traceai_claude_agent_sdk._mcp_tracker import MCP_TOOL_PATTERN

        # Valid patterns
        match = MCP_TOOL_PATTERN.match("mcp__playwright__click")
        assert match is not None
        assert match.group(1) == "playwright"
        assert match.group(2) == "click"

        match = MCP_TOOL_PATTERN.match("mcp__puppeteer__take_screenshot")
        assert match is not None
        assert match.group(1) == "puppeteer"
        assert match.group(2) == "take_screenshot"

        # Invalid patterns
        assert MCP_TOOL_PATTERN.match("Read") is None
        assert MCP_TOOL_PATTERN.match("mcp_playwright_click") is None
        assert MCP_TOOL_PATTERN.match("playwright__click") is None
