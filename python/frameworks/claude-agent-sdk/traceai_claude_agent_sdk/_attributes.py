"""Semantic attributes for Claude Agent SDK instrumentation.

These attributes follow OpenTelemetry semantic conventions where applicable,
with Claude Agent SDK-specific extensions.
"""

from enum import Enum


class ClaudeAgentSpanKind(str, Enum):
    """Span kinds for Claude Agent SDK operations."""

    CONVERSATION = "conversation"  # Root span for a query/conversation
    ASSISTANT_TURN = "assistant_turn"  # Individual assistant response
    TOOL_EXECUTION = "tool_execution"  # Tool invocation
    SUBAGENT = "subagent"  # Subagent (Task tool) execution
    MCP_TOOL = "mcp_tool"  # MCP server tool


class ClaudeAgentAttributes:
    """Semantic attributes for Claude Agent SDK spans.

    Follows OpenTelemetry GenAI semantic conventions where applicable.
    """

    # =========================================================================
    # Span Kind
    # =========================================================================
    SPAN_KIND = "claude_agent.span_kind"

    # =========================================================================
    # Agent/Conversation Attributes
    # =========================================================================
    GEN_AI_AGENT_NAME = "claude_agent.name"
    AGENT_SESSION_ID = "claude_agent.session_id"
    AGENT_PROMPT = "claude_agent.prompt"
    AGENT_SYSTEM_PROMPT = "claude_agent.system_prompt"
    AGENT_MODEL = "claude_agent.model"
    AGENT_PERMISSION_MODE = "claude_agent.permission_mode"
    AGENT_ALLOWED_TOOLS = "claude_agent.allowed_tools"
    AGENT_NUM_TURNS = "claude_agent.num_turns"
    AGENT_IS_RESUMED = "claude_agent.is_resumed"
    AGENT_RESUME_SESSION_ID = "claude_agent.resume_session_id"

    # =========================================================================
    # Tool Attributes
    # =========================================================================
    GEN_AI_TOOL_NAME = "claude_agent.tool.name"
    TOOL_USE_ID = "claude_agent.tool.use_id"
    TOOL_INPUT = "claude_agent.tool.input"
    TOOL_OUTPUT = "claude_agent.tool.output"
    TOOL_IS_ERROR = "claude_agent.tool.is_error"
    TOOL_ERROR_MESSAGE = "claude_agent.tool.error_message"
    TOOL_DURATION_MS = "claude_agent.tool.duration_ms"
    TOOL_SOURCE = "claude_agent.tool.source"  # "builtin", "mcp", "custom"

    # Built-in tool specific attributes
    TOOL_FILE_PATH = "claude_agent.tool.file_path"
    TOOL_COMMAND = "claude_agent.tool.command"
    TOOL_EXIT_CODE = "claude_agent.tool.exit_code"
    TOOL_PATTERN = "claude_agent.tool.pattern"
    TOOL_MATCHES_COUNT = "claude_agent.tool.matches_count"
    TOOL_URL = "claude_agent.tool.url"
    TOOL_SEARCH_QUERY = "claude_agent.tool.search_query"

    # =========================================================================
    # Subagent Attributes
    # =========================================================================
    SUBAGENT_NAME = "claude_agent.subagent.name"
    SUBAGENT_TYPE = "claude_agent.subagent.type"
    SUBAGENT_DESCRIPTION = "claude_agent.subagent.description"
    SUBAGENT_PROMPT = "claude_agent.subagent.prompt"
    SUBAGENT_TOOLS = "claude_agent.subagent.tools"
    PARENT_TOOL_USE_ID = "claude_agent.parent_tool_use_id"

    # =========================================================================
    # Message Attributes
    # =========================================================================
    MESSAGE_TYPE = "claude_agent.message.type"
    MESSAGE_ROLE = "claude_agent.message.role"
    MESSAGE_CONTENT = "claude_agent.message.content"
    MESSAGE_HAS_TOOL_USE = "claude_agent.message.has_tool_use"
    MESSAGE_TOOL_USE_COUNT = "claude_agent.message.tool_use_count"

    # =========================================================================
    # MCP Server Attributes
    # =========================================================================
    MCP_SERVER_NAME = "claude_agent.mcp.server_name"
    MCP_SERVER_COMMAND = "claude_agent.mcp.server_command"
    MCP_SERVER_ARGS = "claude_agent.mcp.server_args"
    MCP_TOOL_COUNT = "claude_agent.mcp.tool_count"

    # =========================================================================
    # Session Attributes
    # =========================================================================
    GEN_AI_CONVERSATION_ID = "claude_agent.session.id"
    SESSION_IS_NEW = "claude_agent.session.is_new"
    SESSION_IS_RESUMED = "claude_agent.session.is_resumed"
    SESSION_PREVIOUS_ID = "claude_agent.session.previous_id"
    SESSION_FORK_FROM = "claude_agent.session.fork_from"

    # =========================================================================
    # Usage/Cost Attributes (GenAI Semantic Conventions)
    # =========================================================================
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
    USAGE_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_tokens"
    USAGE_CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation_tokens"

    # Cost tracking
    COST_TOTAL_USD = "claude_agent.cost.total_usd"
    COST_INPUT_USD = "claude_agent.cost.input_usd"
    COST_OUTPUT_USD = "claude_agent.cost.output_usd"

    # =========================================================================
    # Performance Attributes
    # =========================================================================
    DURATION_MS = "claude_agent.duration_ms"
    DURATION_API_MS = "claude_agent.duration_api_ms"
    TIME_TO_FIRST_TOKEN_MS = "claude_agent.time_to_first_token_ms"

    # =========================================================================
    # Error Attributes
    # =========================================================================
    ERROR_TYPE = "claude_agent.error.type"
    ERROR_MESSAGE = "claude_agent.error.message"
    IS_ERROR = "claude_agent.is_error"

    # =========================================================================
    # Hook Attributes
    # =========================================================================
    HOOK_TYPE = "claude_agent.hook.type"
    HOOK_MATCHER = "claude_agent.hook.matcher"
    HOOK_BLOCKED = "claude_agent.hook.blocked"
    HOOK_MODIFIED = "claude_agent.hook.modified"


# Built-in tool names
BUILTIN_TOOLS = frozenset([
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "AskUserQuestion",
    "Task",
    "NotebookEdit",
    "TodoRead",
    "TodoWrite",
])


def get_tool_source(tool_name: str, mcp_servers: dict = None) -> str:
    """Determine the source of a tool.

    Args:
        tool_name: Name of the tool
        mcp_servers: Dictionary of MCP server configurations

    Returns:
        "builtin", "mcp", or "custom"
    """
    if tool_name in BUILTIN_TOOLS:
        return "builtin"

    if mcp_servers:
        # Check if tool comes from an MCP server
        for server_name, config in mcp_servers.items():
            # MCP tools are typically prefixed with server name
            if tool_name.startswith(f"mcp__{server_name}__"):
                return "mcp"

    return "custom"
