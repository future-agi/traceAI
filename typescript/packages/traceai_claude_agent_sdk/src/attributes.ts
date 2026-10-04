/**
 * Attribute names for Claude Agent SDK spans.
 *
 * The `claude_agent.*` and `gen_ai.usage.*` names are copied from the Python
 * package (`python/frameworks/claude-agent-sdk/traceai_claude_agent_sdk/_attributes.py`)
 * so that both languages emit the same keys. `parity.test.ts` reads that file
 * and fails when a name is missing here.
 */
import { FISpanKind } from "@traceai/fi-semantic-conventions";

/** Span kinds, same strings as the Python `ClaudeAgentSpanKind` enum. */
export const ClaudeAgentSpanKind = {
  CONVERSATION: "conversation",
  ASSISTANT_TURN: "assistant_turn",
  TOOL_EXECUTION: "tool_execution",
  SUBAGENT: "subagent",
  MCP_TOOL: "mcp_tool",
} as const;

export type ClaudeAgentSpanKindValue =
  (typeof ClaudeAgentSpanKind)[keyof typeof ClaudeAgentSpanKind];

/** Same names and values as the Python `ClaudeAgentAttributes` class. */
export const ClaudeAgentAttributes = {
  // Span kind
  SPAN_KIND: "claude_agent.span_kind",

  // Agent / conversation
  GEN_AI_AGENT_NAME: "claude_agent.name",
  AGENT_SESSION_ID: "claude_agent.session_id",
  AGENT_PROMPT: "claude_agent.prompt",
  AGENT_SYSTEM_PROMPT: "claude_agent.system_prompt",
  AGENT_MODEL: "claude_agent.model",
  AGENT_PERMISSION_MODE: "claude_agent.permission_mode",
  AGENT_ALLOWED_TOOLS: "claude_agent.allowed_tools",
  AGENT_NUM_TURNS: "claude_agent.num_turns",
  AGENT_IS_RESUMED: "claude_agent.is_resumed",
  AGENT_RESUME_SESSION_ID: "claude_agent.resume_session_id",

  // Tool
  GEN_AI_TOOL_NAME: "claude_agent.tool.name",
  TOOL_USE_ID: "claude_agent.tool.use_id",
  TOOL_INPUT: "claude_agent.tool.input",
  TOOL_OUTPUT: "claude_agent.tool.output",
  TOOL_IS_ERROR: "claude_agent.tool.is_error",
  TOOL_ERROR_MESSAGE: "claude_agent.tool.error_message",
  TOOL_DURATION_MS: "claude_agent.tool.duration_ms",
  TOOL_SOURCE: "claude_agent.tool.source",

  // Built-in tool specific
  TOOL_FILE_PATH: "claude_agent.tool.file_path",
  TOOL_COMMAND: "claude_agent.tool.command",
  TOOL_EXIT_CODE: "claude_agent.tool.exit_code",
  TOOL_PATTERN: "claude_agent.tool.pattern",
  TOOL_MATCHES_COUNT: "claude_agent.tool.matches_count",
  TOOL_URL: "claude_agent.tool.url",
  TOOL_SEARCH_QUERY: "claude_agent.tool.search_query",

  // Subagent
  SUBAGENT_NAME: "claude_agent.subagent.name",
  SUBAGENT_TYPE: "claude_agent.subagent.type",
  SUBAGENT_DESCRIPTION: "claude_agent.subagent.description",
  SUBAGENT_PROMPT: "claude_agent.subagent.prompt",
  SUBAGENT_TOOLS: "claude_agent.subagent.tools",
  PARENT_TOOL_USE_ID: "claude_agent.parent_tool_use_id",

  // Message
  MESSAGE_TYPE: "claude_agent.message.type",
  MESSAGE_ROLE: "claude_agent.message.role",
  MESSAGE_CONTENT: "claude_agent.message.content",
  MESSAGE_HAS_TOOL_USE: "claude_agent.message.has_tool_use",
  MESSAGE_TOOL_USE_COUNT: "claude_agent.message.tool_use_count",

  // MCP server
  MCP_SERVER_NAME: "claude_agent.mcp.server_name",
  MCP_SERVER_COMMAND: "claude_agent.mcp.server_command",
  MCP_SERVER_ARGS: "claude_agent.mcp.server_args",
  MCP_TOOL_COUNT: "claude_agent.mcp.tool_count",

  // Session
  GEN_AI_CONVERSATION_ID: "claude_agent.session.id",
  SESSION_IS_NEW: "claude_agent.session.is_new",
  SESSION_IS_RESUMED: "claude_agent.session.is_resumed",
  SESSION_PREVIOUS_ID: "claude_agent.session.previous_id",
  SESSION_FORK_FROM: "claude_agent.session.fork_from",

  // Usage (Python names; the cache keys are not the dotted semconv spelling)
  USAGE_INPUT_TOKENS: "gen_ai.usage.input_tokens",
  USAGE_OUTPUT_TOKENS: "gen_ai.usage.output_tokens",
  USAGE_TOTAL_TOKENS: "gen_ai.usage.total_tokens",
  USAGE_CACHE_READ_TOKENS: "gen_ai.usage.cache_read_tokens",
  USAGE_CACHE_CREATION_TOKENS: "gen_ai.usage.cache_creation_tokens",

  // Cost
  COST_TOTAL_USD: "claude_agent.cost.total_usd",
  COST_INPUT_USD: "claude_agent.cost.input_usd",
  COST_OUTPUT_USD: "claude_agent.cost.output_usd",

  // Performance
  DURATION_MS: "claude_agent.duration_ms",
  DURATION_API_MS: "claude_agent.duration_api_ms",
  TIME_TO_FIRST_TOKEN_MS: "claude_agent.time_to_first_token_ms",

  // Error
  ERROR_TYPE: "claude_agent.error.type",
  ERROR_MESSAGE: "claude_agent.error.message",
  IS_ERROR: "claude_agent.is_error",

  // Hook
  HOOK_TYPE: "claude_agent.hook.type",
  HOOK_MATCHER: "claude_agent.hook.matcher",
  HOOK_BLOCKED: "claude_agent.hook.blocked",
  HOOK_MODIFIED: "claude_agent.hook.modified",
} as const;

/**
 * Attributes this package adds on top of the Python list. They exist so the
 * Future AGI collector and trace UI can read the span (span kind, cost,
 * provider, generic tool name) or to record wrapper-only state (cancellation).
 */
export const TraceAIAttributes = {
  /** Span kind key the Python fi_instrumentation package and the collector read. */
  GEN_AI_SPAN_KIND: "gen_ai.span.kind",
  /** Span kind key in TS fi-semantic-conventions (`SemanticConventions.FI_SPAN_KIND`). */
  FI_SPAN_KIND: "fi.span.kind",
  /** Cost key the collector promotes (`gen_ai.cost.total` / `llm.cost.total`). */
  GEN_AI_COST_TOTAL: "gen_ai.cost.total",
  GEN_AI_PROVIDER_NAME: "gen_ai.provider.name",
  GEN_AI_REQUEST_MODEL: "gen_ai.request.model",
  GEN_AI_TOOL_NAME: "gen_ai.tool.name",
  SESSION_ID: "session.id",
  INPUT_VALUE: "input.value",
  INPUT_MIME_TYPE: "input.mime_type",
  OUTPUT_VALUE: "output.value",
  OUTPUT_MIME_TYPE: "output.mime_type",
  /** Set on every span the wrapper closes because the query was aborted. */
  CANCELLED: "claude_agent.cancelled",
  /** MCP tool name without the `mcp__<server>__` prefix (Python `_mcp_tracker.py`). */
  MCP_TOOL_NAME: "claude_agent.mcp.tool_name",
  /** Background task id from `system/task_started`. */
  SUBAGENT_TASK_ID: "claude_agent.subagent.task_id",
  /** Final status from `system/task_notification`. */
  SUBAGENT_STATUS: "claude_agent.subagent.status",
} as const;

/**
 * Future AGI span kind for each Claude Agent span kind.
 *
 * TS `FISpanKind` has no `CONVERSATION` member (Python `FiSpanKindValues` does),
 * so the conversation span falls back to `CHAIN` as the architecture requires.
 * `claude_agent.span_kind` still carries `conversation`.
 */
export const FI_SPAN_KIND_BY_CLAUDE_KIND: Record<ClaudeAgentSpanKindValue, string> = {
  [ClaudeAgentSpanKind.CONVERSATION]:
    (FISpanKind as Record<string, string>)["CONVERSATION"] ?? FISpanKind.CHAIN,
  [ClaudeAgentSpanKind.ASSISTANT_TURN]: FISpanKind.LLM,
  [ClaudeAgentSpanKind.TOOL_EXECUTION]: FISpanKind.TOOL,
  [ClaudeAgentSpanKind.MCP_TOOL]: FISpanKind.TOOL,
  [ClaudeAgentSpanKind.SUBAGENT]: FISpanKind.AGENT,
};

/** Span names (Python `_client_wrapper.py`, `_hooks.py`, `_subagent_tracker.py`). */
export const SpanNames = {
  CONVERSATION: "claude_agent.conversation",
  ASSISTANT_TURN: "claude_agent.assistant_turn",
  toolSpan: (toolName: string) => `tool.${toolName}`,
  subagentSpan: (subagentType: string) => `claude_agent.subagent.${subagentType}`,
} as const;

/**
 * Built-in tool names. Python `BUILTIN_TOOLS` plus `Agent`, the name the
 * 0.3.x SDK uses for the subagent tool (sdk.d.ts: "invoked via the Agent tool").
 */
export const BUILTIN_TOOLS: ReadonlySet<string> = new Set([
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
  "Agent",
]);

/** Tool names that start a subagent. Python checks `Task`; 0.3.x names it `Agent`. */
export const SUBAGENT_TOOLS: ReadonlySet<string> = new Set(["Task", "Agent"]);

export type ToolSource = "builtin" | "mcp" | "custom";

/**
 * Parse `mcp__<server>__<tool>` against the known MCP server names.
 * Returns undefined when the tool is not from a known server.
 */
export function parseMcpToolName(
  toolName: string,
  mcpServerNames: Iterable<string>,
): { server: string; tool: string } | undefined {
  for (const server of mcpServerNames) {
    const prefix = `mcp__${server}__`;
    if (toolName.startsWith(prefix) && toolName.length > prefix.length) {
      return { server, tool: toolName.slice(prefix.length) };
    }
  }
  return undefined;
}

/** Same rule as Python `get_tool_source`. */
export function getToolSource(
  toolName: string,
  mcpServerNames: Iterable<string> = [],
): ToolSource {
  if (BUILTIN_TOOLS.has(toolName)) {
    return "builtin";
  }
  if (parseMcpToolName(toolName, mcpServerNames)) {
    return "mcp";
  }
  return "custom";
}
