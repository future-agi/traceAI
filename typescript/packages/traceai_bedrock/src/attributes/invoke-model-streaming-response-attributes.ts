/**
 * Streaming response attribute extraction for AWS Bedrock InvokeModel instrumentation
 *
 * Handles extraction of semantic convention attributes from streaming InvokeModel responses including:
 * - Streaming content processing
 * - Tool call extraction from streams
 * - Token usage accumulation
 * - Real-time span attribute setting
 * - Consumption-time capture without duplicating SDK streams
 */

import { Span } from "@opentelemetry/api";
import {
  isObjectWithStringKeys,
  safelyJSONStringify,
} from "@traceai/fi-core";
import {
  SemanticConventions,
  LLMSystem,
} from "@traceai/fi-semantic-conventions";
import { setSpanAttribute } from "./attribute-helpers";
import { normalizeUsageAttributes } from "./invoke-model-helpers";
import { UsageAttributes } from "../types/bedrock-types";
import { StreamAccumulator } from "../stream";

/**
 * Interface for raw stream chunks from AWS SDK (network level)
 */
interface StreamChunk {
  chunk: {
    bytes: Uint8Array;
  };
}

/**
 * Valid stream event data structure
 * Covers the common fields across different provider streaming formats
 */
interface StreamEventData {
  type?: string;
  message?: {
    usage?: Record<string, unknown>;
  };
  content_block?: {
    type: string;
    text?: string;
    id?: string;
    name?: string;
    input?: Record<string, unknown>;
  };
  delta?: {
    text?: string;
  };
  usage?: Record<string, unknown>;

  // Amazon-specific fields
  outputText?: string;
  tokenCount?: number;
  "amazon-bedrock-invocationMetrics"?: {
    inputTokenCount?: number;
    outputTokenCount?: number;
  };
  contentBlockDelta?: {
    delta?: {
      text?: string;
    };
  };
  metadata?: {
    usage?: Record<string, unknown>;
  };

  // Meta-specific fields
  generation?: string;
  generation_token_count?: number;
  prompt_token_count?: number;
}

/**
 * Type guard to check if an unknown object is a valid stream chunk
 */
function isValidStreamChunk(chunk: unknown): chunk is StreamChunk {
  return (
    isObjectWithStringKeys(chunk) &&
    isObjectWithStringKeys(chunk.chunk) &&
    chunk.chunk.bytes instanceof Uint8Array
  );
}

/**
 * Type guard to check if parsed JSON data matches expected stream event structure
 */
function isValidStreamEventData(data: unknown): data is StreamEventData {
  return isObjectWithStringKeys(data);
}

/**
 * Stream processing state shared across chunk processors
 * Contains accumulated content and usage data during stream consumption
 */
interface StreamProcessingState {
  outputText: string;
  contentBlocks: Array<{
    type: string;
    text?: string;
    id?: string;
    name?: string;
    input?: Record<string, unknown>;
  }>;
  rawUsageData: Record<string, unknown>;
}

/**
 * Processes Anthropic stream chunks and updates processing state
 * Handles message events, content blocks, and token usage with proper delta accumulation
 *
 * @param data The parsed stream event data
 * @param state Current processing state to update
 * @returns Updated processing state
 */
function processAnthropicStreamChunk(
  data: StreamEventData,
  state: StreamProcessingState,
): StreamProcessingState {
  // Type guard for Anthropic-specific events
  if (!data.type || typeof data.type !== "string") {
    return state;
  }

  if (data.type === "message_start" && data.message?.usage) {
    state.rawUsageData = { ...state.rawUsageData, ...data.message.usage };
  }

  if (data.type === "content_block_start" && data.content_block) {
    // Store tool use blocks for later processing, don't add duplicates
    if (data.content_block.type === "tool_use") {
      state.contentBlocks.push(data.content_block);
    }
  }

  if (data.type === "content_block_delta" && data.delta?.text) {
    // Accumulate all text into the main outputText string
    state.outputText += data.delta.text;
  }

  if (data.type === "message_delta" && data.usage) {
    state.rawUsageData = { ...state.rawUsageData, ...data.usage };
  }

  return state;
}

/**
 * Processes Meta stream chunks and updates processing state
 * Handles generation text and token counting with proper accumulation logic
 *
 * @param data The parsed stream event data
 * @param state Current processing state to update
 * @returns Updated processing state
 */
function processMetaStreamChunk(
  data: StreamEventData,
  state: StreamProcessingState,
): StreamProcessingState {
  // Meta text generation - accumulate all text
  if (typeof data.generation === "string") {
    state.outputText += data.generation;
  }

  // Meta token counting - generation_token_count is total, not incremental
  if (typeof data.generation_token_count === "number") {
    state.rawUsageData.generation_token_count = data.generation_token_count;
  }

  if (typeof data.prompt_token_count === "number") {
    state.rawUsageData.prompt_token_count = data.prompt_token_count;
  }

  return state;
}

/**
 * Type guard to identify Titan streaming format
 * Checks for Titan-specific fields: outputText, tokenCount, or amazon-bedrock-invocationMetrics
 */
function isTitanStreamChunk(data: StreamEventData): boolean {
  return !!(
    typeof data.outputText === "string" ||
    typeof data.tokenCount === "number" ||
    data["amazon-bedrock-invocationMetrics"]
  );
}

/**
 * Type guard to identify Nova streaming format
 * Checks for Nova-specific fields: contentBlockDelta, metadata.usage, or usage with camelCase tokens
 */
function isNovaStreamChunk(data: StreamEventData): boolean {
  return !!(
    data.contentBlockDelta?.delta?.text ||
    data.metadata?.usage ||
    // Nova usage without type field - check for camelCase token names
    (data.usage &&
      typeof data.usage === "object" &&
      !data.type &&
      ("inputTokens" in data.usage ||
        "outputTokens" in data.usage ||
        "cacheReadInputTokenCount" in data.usage ||
        "cacheWriteInputTokenCount" in data.usage))
  );
}

/**
 * Processes Amazon Titan stream chunks and updates processing state
 * Handles Titan-specific fields: outputText, tokenCount, and invocationMetrics
 *
 * @param data The parsed stream event data
 * @param state Current processing state to update
 * @returns Updated processing state
 */
function processTitanStreamChunk(
  data: StreamEventData,
  state: StreamProcessingState,
): StreamProcessingState {
  // Titan text output
  if (typeof data.outputText === "string") {
    state.outputText += data.outputText;
  }

  // Titan incremental token counting
  if (typeof data.tokenCount === "number") {
    state.rawUsageData.outputTokenCount =
      ((state.rawUsageData.outputTokenCount as number) || 0) + data.tokenCount;
  }

  // Titan final metrics (appears at end of stream)
  if (data["amazon-bedrock-invocationMetrics"]) {
    const metrics = data["amazon-bedrock-invocationMetrics"];
    if (typeof metrics.inputTokenCount === "number") {
      state.rawUsageData.inputTextTokenCount = metrics.inputTokenCount;
    }
    if (typeof metrics.outputTokenCount === "number") {
      state.rawUsageData.outputTokenCount = metrics.outputTokenCount;
    }
  }

  return state;
}

/**
 * Processes Amazon Nova stream chunks and updates processing state
 * Handles Nova-specific fields: contentBlockDelta and metadata.usage
 *
 * @param data The parsed stream event data
 * @param state Current processing state to update
 * @returns Updated processing state
 */
function processNovaStreamChunk(
  data: StreamEventData,
  state: StreamProcessingState,
): StreamProcessingState {
  // Nova text output
  if (
    data.contentBlockDelta?.delta?.text &&
    typeof data.contentBlockDelta.delta.text === "string"
  ) {
    state.outputText += data.contentBlockDelta.delta.text;
  }

  // Nova usage from metadata field (common location)
  if (data.metadata?.usage) {
    state.rawUsageData = { ...state.rawUsageData, ...data.metadata.usage };
  }

  // Nova usage at top level (alternative format) - ensure it's not Anthropic
  if (data.usage && typeof data.usage === "object" && !data.type) {
    // Nova provides cache tokens in camelCase format
    const usage = data.usage as Record<string, unknown>;
    state.rawUsageData = { ...state.rawUsageData, ...usage };

    // Specifically capture cache tokens if present
    if (typeof usage.cacheReadInputTokenCount === "number") {
      state.rawUsageData.cacheReadInputTokenCount =
        usage.cacheReadInputTokenCount;
    }
    if (typeof usage.cacheWriteInputTokenCount === "number") {
      state.rawUsageData.cacheWriteInputTokenCount =
        usage.cacheWriteInputTokenCount;
    }
  }

  return state;
}

/**
 * Normalizes accumulated raw usage data into standardized UsageAttributes format
 * Uses the normalizeUsageAttributes helper with appropriate response structure for each provider
 * This is the single source of truth for token normalization - streaming processors only accumulate raw data
 *
 * @param rawUsageData The accumulated raw usage data from stream processing
 * @param modelType The LLM system type to determine normalization strategy
 * @returns Normalized UsageAttributes object
 */
function normalizeStreamUsageData(
  rawUsageData: Record<string, unknown>,
  modelType: LLMSystem,
): UsageAttributes {
  if (modelType === LLMSystem.ANTHROPIC) {
    // Wrap usage data in expected Anthropic format
    const wrappedUsageData = { usage: rawUsageData };
    return normalizeUsageAttributes(wrappedUsageData, modelType) || {};
  }

  if (modelType === LLMSystem.AMAZON) {
    // Auto-detect Nova vs Titan based on accumulated token field names
    const isNovaFormat =
      rawUsageData.inputTokens !== undefined ||
      rawUsageData.outputTokens !== undefined ||
      rawUsageData.cacheReadInputTokenCount !== undefined ||
      rawUsageData.cacheWriteInputTokenCount !== undefined;

    if (isNovaFormat) {
      // Nova format - create structure that passes isNovaResponse check
      // Nova uses camelCase field names (inputTokens, outputTokens, cacheReadInputTokenCount, etc.)
      const responseStructure = {
        output: { message: {} }, // Required for isNovaResponse type guard
        usage: rawUsageData,
      };
      return normalizeUsageAttributes(responseStructure, modelType) || {};
    } else {
      // Titan format - create structure that passes isTitanResponse check
      const responseStructure = {
        inputTextTokenCount: rawUsageData.inputTextTokenCount,
        results: rawUsageData.outputTokenCount
          ? [{ tokenCount: rawUsageData.outputTokenCount }]
          : [],
      };
      return normalizeUsageAttributes(responseStructure, modelType) || {};
    }
  }

  if (modelType === LLMSystem.META) {
    // Meta uses raw usage data directly
    return normalizeUsageAttributes(rawUsageData, modelType) || {};
  }

  // Fallback for unknown providers
  return {};
}

/**
 * Sets streaming output attributes on the OpenTelemetry span
 * Processes accumulated content blocks and usage data into semantic convention attributes
 *
 * @param params Object containing span and accumulated streaming data
 */
function setStreamingOutputAttributes({
  span,
  outputText,
  contentBlocks,
  usage,
}: {
  span: Span;
  outputText: string;
  contentBlocks: StreamProcessingState["contentBlocks"];
  usage: UsageAttributes;
}): void {
  // Create the output value structure like the original streaming code
  const toolCalls = contentBlocks
    .filter((block) => block.type === "tool_use")
    .map((block) => ({
      id: block.id || "unknown",
      name: block.name || "unknown",
      input: block.input || {},
    }));

  const outputValue = {
    text: outputText || "",
    tool_calls: toolCalls,
    usage: usage || {},
    streaming: true,
  };

  // Set output value as JSON
  setSpanAttribute(
    span,
    SemanticConventions.OUTPUT_VALUE,
    JSON.stringify(outputValue),
  );
  setSpanAttribute(
    span,
    SemanticConventions.OUTPUT_MIME_TYPE,
    "application/json",
  );

  // Build output message as JSON blob
  const outputMessage: Record<string, unknown> = { role: "assistant" };
  if (outputText) {
    outputMessage.content = outputText;
  }

  const toolUseBlocks = contentBlocks.filter(
    (block) => block.type === "tool_use",
  );
  if (toolUseBlocks.length > 0) {
    outputMessage.tool_calls = toolUseBlocks.map((block) => ({
      id: block.id || "unknown",
      function: {
        name: block.name || "unknown",
        arguments: JSON.stringify(block.input || {}),
      },
    }));
  }

  setSpanAttribute(
    span,
    SemanticConventions.LLM_OUTPUT_MESSAGES,
    safelyJSONStringify([outputMessage]) ?? "[]",
  );

  // Set usage attributes
  if (usage.input_tokens !== undefined) {
    setSpanAttribute(
      span,
      SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
      usage.input_tokens,
    );
  }
  if (usage.output_tokens !== undefined) {
    setSpanAttribute(
      span,
      SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
      usage.output_tokens,
    );
  }
  const totalTokens = usage.total_tokens ?? (
    usage.input_tokens !== undefined && usage.output_tokens !== undefined
      ? usage.input_tokens + usage.output_tokens
      : undefined
  );
  if (totalTokens !== undefined) {
    setSpanAttribute(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL, totalTokens);
  }
  if (usage.cache_read_input_tokens !== undefined) {
    setSpanAttribute(
      span,
      SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
      usage.cache_read_input_tokens,
    );
  }
  if (usage.cache_creation_input_tokens !== undefined) {
    setSpanAttribute(
      span,
      SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE,
      usage.cache_creation_input_tokens,
    );
  }
}

/** Accumulate events as the application consumes them, including partial output on close. */
export function createInvokeModelStreamAccumulator(
  span: Span,
  modelType: LLMSystem,
): StreamAccumulator {
  const state: StreamProcessingState = {
    outputText: "",
    contentBlocks: [],
    rawUsageData: {},
  };
  return {
    add(chunk) {
      if (!isValidStreamChunk(chunk)) return;
      const text = new TextDecoder().decode(chunk.chunk.bytes);
      for (const line of text.split("\n").filter((line) => line.trim())) {
        let data: unknown;
        try { data = JSON.parse(line); } catch { continue; }
        if (!isValidStreamEventData(data)) continue;
        if (modelType === LLMSystem.ANTHROPIC) {
          processAnthropicStreamChunk(data, state);
        } else if (modelType === LLMSystem.AMAZON) {
          if (isTitanStreamChunk(data)) processTitanStreamChunk(data, state);
          else if (isNovaStreamChunk(data)) processNovaStreamChunk(data, state);
        } else if (modelType === LLMSystem.META) {
          processMetaStreamChunk(data, state);
        }
      }
    },
    finish() {
      setStreamingOutputAttributes({
        span,
        outputText: state.outputText,
        contentBlocks: state.contentBlocks,
        usage: normalizeStreamUsageData(state.rawUsageData, modelType),
      });
    },
  };
}
