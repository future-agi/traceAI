/**
 * Span names and attribute keys for Cursor SDK spans.
 *
 * Standard keys (`fi.span.kind`, `session.id`, `input.value`, token counts...)
 * come from `@traceai/fi-semantic-conventions` so the Future AGI collector
 * promotes them. `cursor.*` keys carry the SDK's own structural fields.
 */
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

export const SpanNames = {
  /** One `agent.send()` run (also each run inside `Agent.prompt()`). */
  RUN: "Cursor Agent run",
  /** `Agent.create()` that threw (bad API key, bad model...). Successful creates emit no span. */
  CREATE: "Cursor Agent create",
  /** `Agent.resume()` that threw. Successful resumes emit no span. */
  RESUME: "Cursor Agent resume",
  /** Prefix of a tool span: `Cursor tool: read`. */
  TOOL_PREFIX: "Cursor tool: ",
  THINKING: "Cursor thinking",
  ASSISTANT_MESSAGE: "Cursor assistant message",
} as const;

export const CursorAttributes = {
  AGENT_ID: "cursor.agent.id",
  RUNTIME: "cursor.runtime",
  RUN_ID: "cursor.run.id",
  REQUEST_ID: "cursor.request.id",
  RUN_STATUS: "cursor.run.status",
  RUN_DURATION_MS: "cursor.run.duration_ms",
  RUN_ERROR_CODE: "cursor.run.error_code",
  RUN_CANCELLED: "cursor.run.cancelled",
  /** The consumer stopped iterating `run.stream()` before the run ended. */
  RUN_STREAM_DETACHED: "cursor.run.stream_detached",
  /** Last `status` stream event (CREATING, RUNNING, FINISHED, ERROR, CANCELLED, EXPIRED). */
  LAST_STATUS_EVENT: "cursor.run.last_status_event",
  SEND_MODE: "cursor.send.mode",
  MODEL_PARAMS: "cursor.model.params",
  TOOLS: "cursor.tools",
  INPUT_LENGTH: "cursor.input.length",
  INPUT_IMAGE_COUNT: "cursor.input.image_count",

  TOOL_CALL_ID: "cursor.tool.call_id",
  TOOL_STATUS: "cursor.tool.status",
  TOOL_ARGS_TRUNCATED: "cursor.tool.args_truncated",
  TOOL_RESULT_TRUNCATED: "cursor.tool.result_truncated",
  /** The run ended (or the consumer stopped reading) before the tool's completion event. */
  TOOL_INCOMPLETE: "cursor.tool.incomplete",
  /** false when the SDK sent only the completion event; the span then starts at the previous event. */
  TOOL_STARTED_EVENT: "cursor.tool.started_event",

  THINKING_DURATION_MS: "cursor.thinking.duration_ms",
  TEXT_LENGTH: "cursor.text.length",
  ASSISTANT_TOOL_USE_COUNT: "cursor.assistant.tool_use_count",

  USAGE_INPUT_TOKENS: "cursor.usage.input_tokens",
  USAGE_OUTPUT_TOKENS: "cursor.usage.output_tokens",
  USAGE_CACHE_READ_TOKENS: "cursor.usage.cache_read_tokens",
  USAGE_CACHE_WRITE_TOKENS: "cursor.usage.cache_write_tokens",
  USAGE_TOTAL_TOKENS: "cursor.usage.total_tokens",
  USAGE_REASONING_TOKENS: "cursor.usage.reasoning_tokens",
  /** `run` when the SDK's cumulative run usage was used, `events` when summed from `usage` stream events. */
  USAGE_SOURCE: "cursor.usage.source",

  /** SDK error fields (`CursorSdkError`): never content. */
  ERROR_NAME: "cursor.error.name",
  ERROR_CODE: "cursor.error.code",
  ERROR_STATUS: "cursor.error.http_status",
  ERROR_RETRYABLE: "cursor.error.retryable",
  ERROR_OPERATION: "cursor.error.operation",
  ERROR_MESSAGE: "cursor.error.message",
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

export const AGENT_NAME = "cursor";
/** All inference goes through Cursor's hosted models, local or cloud runtime. */
export const PROVIDER_NAME = "cursor";
