import { SemanticConventions } from "@traceai/fi-semantic-conventions";

/**
 * Attribute keys written by @voltagent/core 2.11.0.
 *
 * Source: the published 2.11.0 bundle (sources recovered from dist/index.js.map):
 * - src/agent/open-telemetry/trace-context.ts (root + child span attributes)
 * - src/agent/agent.ts (llm, tool, memory.read, retriever spans)
 * - src/memory/manager/memory-manager.ts (memory.write, embedding, vector spans)
 */
export const VoltAgentAttributes = {
  ENTITY_TYPE: "entity.type",
  ENTITY_ID: "entity.id",
  ENTITY_NAME: "entity.name",
  SPAN_TYPE: "span.type",
  OPERATION_ID: "operation.id",
  AGENT_STATE: "agent.state",
  CONVERSATION_ID: "conversation.id",
  USER_ID: "user.id",

  // Root (agent) span model + summed usage (trace-context.ts setModelAttributes/setUsage)
  AI_MODEL_NAME: "ai.model.name",
  AI_MODEL_PROVIDER: "ai.model.provider",
  AI_MODEL_TEMPERATURE: "ai.model.temperature",
  AI_MODEL_MAX_TOKENS: "ai.model.max_tokens",
  AI_MODEL_TOP_P: "ai.model.top_p",
  AI_RESPONSE_FINISH_REASON: "ai.response.finish_reason",
  USAGE_PROMPT_TOKENS: "usage.prompt_tokens",
  USAGE_COMPLETION_TOKENS: "usage.completion_tokens",
  USAGE_TOTAL_TOKENS: "usage.total_tokens",
  USAGE_CACHED_TOKENS: "usage.cached_tokens",
  USAGE_REASONING_TOKENS: "usage.reasoning_tokens",

  // LLM span (agent.ts buildLLMSpanAttributes / recordLLMUsage)
  LLM_OPERATION: "llm.operation",
  LLM_MODEL: "llm.model",
  LLM_PROVIDER: "llm.provider",
  LLM_TEMPERATURE: "llm.temperature",
  LLM_MAX_OUTPUT_TOKENS: "llm.max_output_tokens",
  LLM_TOP_P: "llm.top_p",
  LLM_FINISH_REASON: "llm.finish_reason",
  LLM_MODEL_RESOLUTION_FAILED: "llm.model_resolution_failed",
  LLM_USAGE_PROMPT_TOKENS: "llm.usage.prompt_tokens",
  LLM_USAGE_COMPLETION_TOKENS: "llm.usage.completion_tokens",
  LLM_USAGE_TOTAL_TOKENS: "llm.usage.total_tokens",
  LLM_USAGE_CACHED_TOKENS: "llm.usage.cached_tokens",
  LLM_USAGE_REASONING_TOKENS: "llm.usage.reasoning_tokens",

  // Tool span (agent.ts tool.execution:<name>)
  TOOL_NAME: "tool.name",
  TOOL_CALL_ID: "tool.call.id",
  TOOL_DESCRIPTION: "tool.description",

  // Memory spans
  MEMORY_OPERATION: "memory.operation",

  // Embedding / vector vocabulary (architecture: span-helpers.ts on main; not in 2.11.0)
  EMBEDDING_QUERY: "embedding.query",
  VECTOR_QUERY: "vector.query",

  INPUT: "input",
  OUTPUT: "output",
} as const;

/** OpenTelemetry GenAI / Future AGI keys this package writes. */
export const FIAttributes = {
  FI_SPAN_KIND: SemanticConventions.FI_SPAN_KIND, // "fi.span.kind"
  GEN_AI_SPAN_KIND: "gen_ai.span.kind",
  OPENINFERENCE_SPAN_KIND: "openinference.span.kind",
  GEN_AI_OPERATION_NAME: SemanticConventions.GEN_AI_OPERATION_NAME,
  SESSION_ID: SemanticConventions.SESSION_ID, // "session.id"
  USER_ID: SemanticConventions.USER_ID, // "user.id"
  GEN_AI_CONVERSATION_ID: SemanticConventions.GEN_AI_CONVERSATION_ID,
  GEN_AI_REQUEST_MODEL: SemanticConventions.LLM_MODEL_NAME, // "gen_ai.request.model"
  GEN_AI_RESPONSE_MODEL: SemanticConventions.GEN_AI_RESPONSE_MODEL,
  GEN_AI_PROVIDER_NAME: SemanticConventions.LLM_PROVIDER, // "gen_ai.provider.name"
  GEN_AI_REQUEST_TEMPERATURE: "gen_ai.request.temperature",
  GEN_AI_REQUEST_MAX_TOKENS: "gen_ai.request.max_tokens",
  GEN_AI_REQUEST_TOP_P: "gen_ai.request.top_p",
  GEN_AI_RESPONSE_FINISH_REASONS: SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS,
  GEN_AI_USAGE_INPUT_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
  GEN_AI_USAGE_OUTPUT_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
  GEN_AI_USAGE_TOTAL_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
  // Cache/reasoning: the OTel GenAI name (architecture) plus the traceAI TS constant.
  GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: "gen_ai.usage.cache_read.input_tokens",
  FI_USAGE_CACHE_READ_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
  GEN_AI_USAGE_REASONING_OUTPUT_TOKENS: "gen_ai.usage.reasoning.output_tokens",
  FI_USAGE_REASONING_TOKENS: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
  GEN_AI_TOOL_NAME: "gen_ai.tool.name",
  GEN_AI_TOOL_CALL_ID: "gen_ai.tool.call.id",
  GEN_AI_TOOL_DESCRIPTION: "gen_ai.tool.description",
  INPUT_VALUE: SemanticConventions.INPUT_VALUE, // "input.value"
  OUTPUT_VALUE: SemanticConventions.OUTPUT_VALUE, // "output.value"
  INPUT_MIME_TYPE: SemanticConventions.INPUT_MIME_TYPE,
  OUTPUT_MIME_TYPE: SemanticConventions.OUTPUT_MIME_TYPE,
} as const;

/** Keys this package adds under its own namespace. */
export const VoltAgentFIAttributes = {
  /** Summed usage from a non-model span (agent root), moved off the promoted keys. */
  USAGE_PREFIX: "voltagent.usage.",
  /** Last-step usage VoltAgent recorded on an llm span before reconciliation. */
  LAST_STEP_USAGE_PREFIX: "voltagent.llm.last_step_usage.",
  USAGE_RECONCILED: "voltagent.usage.reconciled",
  /** Original span.type / entity.type that produced the kind. */
  SOURCE_SPAN_TYPE: "voltagent.span_type",
} as const;
