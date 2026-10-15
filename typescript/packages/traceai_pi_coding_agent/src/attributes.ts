/**
 * Span names and attribute keys for Pi Coding Agent SDK spans.
 *
 * Standard keys come from `@traceai/fi-semantic-conventions` and the OTel
 * GenAI conventions. `pi.*` keys carry Pi-specific facts the standard keys do
 * not cover.
 */
import { FISpanKind, SemanticConventions } from "@traceai/fi-semantic-conventions";

export const PI_AGENT_NAME = "pi";

export const SpanNames = {
  /**
   * One agent run: from `agent_start` until `agent_settled` (OTel GenAI
   * `invoke_agent {agent}`). Pi before 0.80.5 has no `agent_settled`; the run
   * then ends at an `agent_end` without `willRetry`.
   */
  AGENT_RUN: `invoke_agent ${PI_AGENT_NAME}`,
  /** One `turn_start` .. `turn_end`: one model response plus the tools it called. */
  TURN: "pi.turn",
  /** One assistant message (OTel GenAI `chat {model}`). */
  llm: (model: string | undefined) => (model ? `chat ${model}` : "chat"),
  /** One tool execution (OTel GenAI `execute_tool {tool}`). */
  tool: (toolName: string) => `execute_tool ${toolName}`,
  /** One context compaction: `compaction_start` .. `compaction_end`. */
  COMPACTION: "pi.compaction",
} as const;

export const Attr = {
  FI_SPAN_KIND: SemanticConventions.FI_SPAN_KIND,
  /** Span kind key the Future AGI collector reads when `gen_ai.*` keys are present. */
  GEN_AI_SPAN_KIND: "gen_ai.span.kind",
  GEN_AI_OPERATION_NAME: SemanticConventions.GEN_AI_OPERATION_NAME,
  GEN_AI_AGENT_NAME: "gen_ai.agent.name",
  GEN_AI_CONVERSATION_ID: SemanticConventions.GEN_AI_CONVERSATION_ID,
  SESSION_ID: SemanticConventions.SESSION_ID,
  INPUT_VALUE: SemanticConventions.INPUT_VALUE,
  INPUT_MIME_TYPE: SemanticConventions.INPUT_MIME_TYPE,
  OUTPUT_VALUE: SemanticConventions.OUTPUT_VALUE,
  OUTPUT_MIME_TYPE: SemanticConventions.OUTPUT_MIME_TYPE,
  /** OTel `error.type`: the error class on ERROR spans (provider error type, HTTP status, `aborted`, `tool_error`). */
  ERROR_TYPE: "error.type",

  // LLM
  REQUEST_MODEL: SemanticConventions.LLM_MODEL_NAME,
  RESPONSE_MODEL: SemanticConventions.GEN_AI_RESPONSE_MODEL,
  RESPONSE_ID: SemanticConventions.GEN_AI_RESPONSE_ID,
  FINISH_REASONS: SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS,
  PROVIDER_NAME: SemanticConventions.LLM_PROVIDER,
  INPUT_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
  OUTPUT_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
  TOTAL_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
  CACHE_READ_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
  CACHE_WRITE_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE,
  REASONING_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
  /** Cost key the collector promotes. Written only when Pi priced the call (cost > 0). */
  COST_TOTAL: "gen_ai.cost.total",

  // Tool
  GEN_AI_TOOL_NAME: "gen_ai.tool.name",
  GEN_AI_TOOL_CALL_ID: "gen_ai.tool.call.id",
  TOOL_NAME: SemanticConventions.TOOL_NAME,
  TOOL_CALL_ID: SemanticConventions.TOOL_CALL_ID,

  // Pi-specific
  PI_SESSION_ID: "pi.session.id",
  PI_SESSION_NAME: "pi.session.name",
  PI_API: "pi.api",
  PI_THINKING_LEVEL: "pi.thinking_level",
  PI_STOP_REASON: "pi.stop_reason",
  PI_ABORTED: "pi.aborted",
  /** Pi's own `usage.input`: input tokens that were neither cache reads nor cache writes. */
  PI_UNCACHED_INPUT_TOKENS: "pi.usage.uncached_input_tokens",
  PI_TOOL_CALL_NAMES: "pi.llm.tool_call_names",
  PI_TOOL_CALL_COUNT: "pi.llm.tool_call_count",
  PI_TURN_INDEX: "pi.turn.index",
  PI_TURN_TOOL_RESULT_COUNT: "pi.turn.tool_result_count",
  PI_TOOL_DURATION_MS: "pi.tool.duration_ms",
  PI_TOOL_PARENT_CALL_ID: "pi.tool.parent_call_id",
  PI_TOOL_IS_ERROR: "pi.tool.is_error",
  /**
   * Agent loops (`agent_start` events) in the run: the first, each automatic
   * retry, and each continuation (overflow-compaction recovery, queued messages).
   */
  PI_RUN_ATTEMPTS: "pi.agent.attempts",
  /** Automatic retries in the run (`auto_retry_start` events). */
  PI_RUN_RETRY_COUNT: "pi.agent.retry_count",
  PI_RUN_TURN_COUNT: "pi.agent.turn_count",
  PI_RUN_LLM_CALL_COUNT: "pi.agent.llm_call_count",
  PI_RUN_TOOL_CALL_COUNT: "pi.agent.tool_call_count",
  /**
   * Token totals for the whole run (its LLM and compaction spans). Not the
   * promoted `gen_ai.usage.*` keys, so the collector does not count the run's
   * tokens twice (once per child span and once on the run).
   */
  PI_RUN_INPUT_TOKENS: "pi.agent.usage.input_tokens",
  PI_RUN_OUTPUT_TOKENS: "pi.agent.usage.output_tokens",
  PI_RUN_TOTAL_TOKENS: "pi.agent.usage.total_tokens",
  PI_RUN_COST_TOTAL: "pi.agent.cost.total",
  PI_COMPACTION_REASON: "pi.compaction.reason",
  PI_COMPACTION_TOKENS_BEFORE: "pi.compaction.tokens_before",
  PI_COMPACTION_TOKENS_AFTER: "pi.compaction.estimated_tokens_after",
  PI_COMPACTION_WILL_RETRY: "pi.compaction.will_retry",
  /** Set when the span was closed by `dispose()` / `unsubscribe()` before Pi finished it. */
  PI_INCOMPLETE: "pi.incomplete",
} as const;

export const SpanKind = {
  AGENT: FISpanKind.AGENT,
  CHAIN: FISpanKind.CHAIN,
  LLM: FISpanKind.LLM,
  TOOL: FISpanKind.TOOL,
} as const;

/** Span event names recorded on the run span. */
export const EventNames = {
  AUTO_RETRY_START: "pi.auto_retry.start",
  AUTO_RETRY_END: "pi.auto_retry.end",
} as const;
