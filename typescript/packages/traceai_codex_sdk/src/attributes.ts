/**
 * Span names and attribute keys for Codex SDK spans.
 *
 * Standard keys (`fi.span.kind`, `session.id`, `input.value`, token counts...)
 * come from `@traceai/fi-semantic-conventions` so the Future AGI collector
 * promotes them. `codex.*` keys carry the SDK's own structural fields.
 */
import { FISpanKind, SemanticConventions } from "@traceai/fi-semantic-conventions";

export const SpanNames = {
  TURN: "codex.turn",
  AGENT_MESSAGE: "codex.agent_message",
  REASONING: "codex.reasoning",
  COMMAND_EXECUTION: "codex.command_execution",
  FILE_CHANGE: "codex.file_change",
  MCP_TOOL_CALL: "codex.mcp_tool_call",
  WEB_SEARCH: "codex.web_search",
  TODO_LIST: "codex.todo_list",
  ERROR: "codex.error",
} as const;

/** SDK item type -> span name and Future AGI span kind. */
export const ITEM_SPANS: Record<string, { name: string; kind: FISpanKind; tool: boolean }> = {
  agent_message: { name: SpanNames.AGENT_MESSAGE, kind: FISpanKind.LLM, tool: false },
  reasoning: { name: SpanNames.REASONING, kind: FISpanKind.CHAIN, tool: false },
  command_execution: { name: SpanNames.COMMAND_EXECUTION, kind: FISpanKind.TOOL, tool: true },
  file_change: { name: SpanNames.FILE_CHANGE, kind: FISpanKind.TOOL, tool: true },
  mcp_tool_call: { name: SpanNames.MCP_TOOL_CALL, kind: FISpanKind.TOOL, tool: true },
  web_search: { name: SpanNames.WEB_SEARCH, kind: FISpanKind.TOOL, tool: true },
  todo_list: { name: SpanNames.TODO_LIST, kind: FISpanKind.CHAIN, tool: false },
  // Non-fatal: status UNSET with codex.warning=true.
  error: { name: SpanNames.ERROR, kind: FISpanKind.CHAIN, tool: false },
};

/** Fallback for an item type a newer SDK adds. */
export const UNKNOWN_ITEM_SPAN = { kind: FISpanKind.CHAIN, tool: false };

export const CodexAttributes = {
  THREAD_ID: "codex.thread.id",
  THREAD_RESUMED: "codex.thread.resumed",
  SANDBOX_MODE: "codex.sandbox_mode",
  APPROVAL_POLICY: "codex.approval_policy",
  REASONING_EFFORT: "codex.model_reasoning_effort",
  WEB_SEARCH_MODE: "codex.web_search_mode",
  INPUT_IMAGE_COUNT: "codex.input.image_count",
  INPUT_LENGTH: "codex.input.length",

  ITEM_ID: "codex.item.id",
  ITEM_TYPE: "codex.item.type",
  ITEM_STATUS: "codex.item.status",
  /** false when the SDK sent only `item.completed`; the span then starts at the previous event. */
  ITEM_STARTED_EVENT: "codex.item.started_event",
  ITEM_INCOMPLETE: "codex.item.incomplete",

  COMMAND_EXIT_CODE: "codex.command.exit_code",
  COMMAND_OUTPUT_LENGTH: "codex.command.output_length",
  FILE_CHANGE_COUNT: "codex.file_change.count",
  FILE_CHANGE_KINDS: "codex.file_change.kinds",
  MCP_SERVER: "codex.mcp.server",
  MCP_TOOL: "codex.mcp.tool",
  TODO_COUNT: "codex.todo.count",
  TODO_COMPLETED: "codex.todo.completed",
  TEXT_LENGTH: "codex.text.length",

  USAGE_CACHED_INPUT_TOKENS: "codex.usage.cached_input_tokens",
  USAGE_CACHE_WRITE_INPUT_TOKENS: "codex.usage.cache_write_input_tokens",
  USAGE_REASONING_OUTPUT_TOKENS: "codex.usage.reasoning_output_tokens",
  /** A resumed thread's first turn in this process: no earlier total, so nothing promoted. */
  USAGE_BASELINE_UNKNOWN: "codex.usage.baseline_unknown",
  /** The running total went down; the reported value was promoted as is. */
  USAGE_NON_MONOTONIC: "codex.usage.non_monotonic",
  /** Prefix for the thread's running totals, e.g. `codex.cumulative.input_tokens`. */
  CUMULATIVE_PREFIX: "codex.cumulative.",

  /** Fixed summary unless outputs are visible; see README "Privacy". */
  ERROR_MESSAGE: "codex.error.message",
  /** Error class name of a thrown error, e.g. `Error`, `TypeError`. */
  ERROR_TYPE: "codex.error.type",
  /** `error` events in the turn. Codex sends them for errors it retries; the turn can still complete. */
  STREAM_ERROR_COUNT: "codex.stream_error.count",
  /** On `codex.error` spans: a non-fatal error item (warning, deprecation, model reroute). */
  WARNING: "codex.warning",
  CANCELLED: "codex.cancelled",
} as const;

/** Standard keys used by this package, resolved from fi-semantic-conventions. */
export const StandardAttributes = {
  FI_SPAN_KIND: SemanticConventions.FI_SPAN_KIND,
  /** Same value as `fi.span.kind`; the Python fi_instrumentation key the collector also reads. */
  GEN_AI_SPAN_KIND: "gen_ai.span.kind",
  SESSION_ID: SemanticConventions.SESSION_ID,
  CONVERSATION_ID: SemanticConventions.GEN_AI_CONVERSATION_ID,
  INPUT_VALUE: SemanticConventions.INPUT_VALUE,
  INPUT_MIME_TYPE: SemanticConventions.INPUT_MIME_TYPE,
  OUTPUT_VALUE: SemanticConventions.OUTPUT_VALUE,
  OUTPUT_MIME_TYPE: SemanticConventions.OUTPUT_MIME_TYPE,
  MODEL_NAME: SemanticConventions.LLM_MODEL_NAME,
  PROVIDER: SemanticConventions.LLM_PROVIDER,
  TOOL_NAME: SemanticConventions.TOOL_NAME,
  GEN_AI_TOOL_NAME: "gen_ai.tool.name",
  TOOL_CALL_ID: "gen_ai.tool.call.id",
  AGENT_NAME: "gen_ai.agent.name",
  INPUT_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
  OUTPUT_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
  TOTAL_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
  CACHE_READ_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
  CACHE_WRITE_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE,
  REASONING_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
} as const;

export const AGENT_NAME = "codex";
/** Codex talks to the OpenAI Responses API (or an OpenAI-compatible proxy). */
export const PROVIDER_NAME = "openai";
