"""Tests for Claude Agent SDK semantic attributes."""

import pytest


class TestClaudeAgentSpanKind:
    """Test ClaudeAgentSpanKind enum."""

    def test_conversation_value(self):
        """Test conversation span kind value."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentSpanKind

        assert ClaudeAgentSpanKind.CONVERSATION.value == "conversation"

    def test_assistant_turn_value(self):
        """Test assistant turn span kind value."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentSpanKind

        assert ClaudeAgentSpanKind.ASSISTANT_TURN.value == "assistant_turn"

    def test_tool_execution_value(self):
        """Test tool execution span kind value."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentSpanKind

        assert ClaudeAgentSpanKind.TOOL_EXECUTION.value == "tool_execution"

    def test_subagent_value(self):
        """Test subagent span kind value."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentSpanKind

        assert ClaudeAgentSpanKind.SUBAGENT.value == "subagent"

    def test_mcp_tool_value(self):
        """Test MCP tool span kind value."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentSpanKind

        assert ClaudeAgentSpanKind.MCP_TOOL.value == "mcp_tool"


class TestClaudeAgentAttributes:
    """Test ClaudeAgentAttributes class."""

    def test_span_kind_attribute(self):
        """Test span kind attribute."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.SPAN_KIND == "claude_agent.span_kind"

    def test_agent_attributes(self):
        """Test agent-level attributes."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.GEN_AI_AGENT_NAME == "claude_agent.name"
        assert ClaudeAgentAttributes.AGENT_SESSION_ID == "claude_agent.session_id"
        assert ClaudeAgentAttributes.AGENT_PROMPT == "claude_agent.prompt"
        assert ClaudeAgentAttributes.AGENT_MODEL == "claude_agent.model"
        assert ClaudeAgentAttributes.AGENT_PERMISSION_MODE == "claude_agent.permission_mode"

    def test_tool_attributes(self):
        """Test tool-level attributes."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.GEN_AI_TOOL_NAME == "claude_agent.tool.name"
        assert ClaudeAgentAttributes.TOOL_USE_ID == "claude_agent.tool.use_id"
        assert ClaudeAgentAttributes.TOOL_INPUT == "claude_agent.tool.input"
        assert ClaudeAgentAttributes.TOOL_OUTPUT == "claude_agent.tool.output"
        assert ClaudeAgentAttributes.TOOL_IS_ERROR == "claude_agent.tool.is_error"

    def test_subagent_attributes(self):
        """Test subagent attributes."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.SUBAGENT_NAME == "claude_agent.subagent.name"
        assert ClaudeAgentAttributes.SUBAGENT_TYPE == "claude_agent.subagent.type"
        assert ClaudeAgentAttributes.PARENT_TOOL_USE_ID == "claude_agent.parent_tool_use_id"

    def test_usage_attributes(self):
        """Test usage/cost attributes."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
        assert ClaudeAgentAttributes.USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
        assert ClaudeAgentAttributes.COST_TOTAL_USD == "claude_agent.cost.total_usd"

    def test_session_attributes(self):
        """Test session attributes."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.GEN_AI_CONVERSATION_ID == "claude_agent.session.id"
        assert ClaudeAgentAttributes.SESSION_IS_RESUMED == "claude_agent.session.is_resumed"

    def test_mcp_attributes(self):
        """Test MCP server attributes."""
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        assert ClaudeAgentAttributes.MCP_SERVER_NAME == "claude_agent.mcp.server_name"
        assert ClaudeAgentAttributes.MCP_SERVER_COMMAND == "claude_agent.mcp.server_command"


class TestBuiltinTools:
    """Test built-in tools set."""

    def test_builtin_tools_contains_core_tools(self):
        """Test that core tools are in the builtin set."""
        from traceai_claude_agent_sdk._attributes import BUILTIN_TOOLS

        assert "Read" in BUILTIN_TOOLS
        assert "Write" in BUILTIN_TOOLS
        assert "Edit" in BUILTIN_TOOLS
        assert "Bash" in BUILTIN_TOOLS
        assert "Glob" in BUILTIN_TOOLS
        assert "Grep" in BUILTIN_TOOLS

    def test_builtin_tools_contains_web_tools(self):
        """Test that web tools are in the builtin set."""
        from traceai_claude_agent_sdk._attributes import BUILTIN_TOOLS

        assert "WebSearch" in BUILTIN_TOOLS
        assert "WebFetch" in BUILTIN_TOOLS

    def test_builtin_tools_contains_task(self):
        """Test that Task tool is in the builtin set."""
        from traceai_claude_agent_sdk._attributes import BUILTIN_TOOLS

        assert "Task" in BUILTIN_TOOLS

    def test_builtin_tools_contains_ask_user(self):
        """Test that AskUserQuestion is in the builtin set."""
        from traceai_claude_agent_sdk._attributes import BUILTIN_TOOLS

        assert "AskUserQuestion" in BUILTIN_TOOLS

    def test_builtin_tools_is_frozenset(self):
        """Test that BUILTIN_TOOLS is immutable."""
        from traceai_claude_agent_sdk._attributes import BUILTIN_TOOLS

        assert isinstance(BUILTIN_TOOLS, frozenset)


class TestGetToolSource:
    """Test get_tool_source function."""

    def test_builtin_tool_returns_builtin(self):
        """Test that built-in tools return 'builtin'."""
        from traceai_claude_agent_sdk._attributes import get_tool_source

        assert get_tool_source("Read") == "builtin"
        assert get_tool_source("Bash") == "builtin"
        assert get_tool_source("Task") == "builtin"

    def test_unknown_tool_returns_custom(self):
        """Test that unknown tools return 'custom'."""
        from traceai_claude_agent_sdk._attributes import get_tool_source

        assert get_tool_source("MyCustomTool") == "custom"
        assert get_tool_source("unknown") == "custom"

    def test_mcp_tool_returns_mcp(self):
        """Test that MCP tools return 'mcp'."""
        from traceai_claude_agent_sdk._attributes import get_tool_source

        mcp_servers = {"playwright": {"command": "npx"}}
        assert get_tool_source("mcp__playwright__click", mcp_servers) == "mcp"

    def test_no_mcp_servers_returns_custom(self):
        """Test with no MCP servers configured."""
        from traceai_claude_agent_sdk._attributes import get_tool_source

        assert get_tool_source("mcp__playwright__click", None) == "custom"
        assert get_tool_source("mcp__playwright__click", {}) == "custom"
