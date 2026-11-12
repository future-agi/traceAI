/**
 * Streaming response attribute extraction for AWS Bedrock InvokeModel instrumentation
 *
 * Handles extraction of semantic convention attributes from streaming InvokeModel responses including:
 * - Streaming content processing
 * - Tool call extraction from streams
 * - Token usage accumulation
 * - Real-time span attribute setting
 * - Pull-driven observation with upstream iterator lifecycle preservation
 */

import { Span, SpanStatusCode, diag } from "@opentelemetry/api";
import {
  withSafety,
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
import { PassThrough } from "stream";

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
  index?: number;
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
    type?: string;
    text?: string;
    partial_json?: string;
    stop_reason?: string;
  };
  usage?: Record<string, unknown>;

  stop_reason?: string;
  completionReason?: string;
  messageStop?: { stopReason?: string };

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
    input?: Record<string, unknown> | string;
    partialJson?: string;
    argumentsIncomplete?: boolean;
  }>;
  toolBlocksByIndex: Map<number, StreamProcessingState["contentBlocks"][number]>;
  rawUsageData: Record<string, unknown>;
  finishReason?: string;
}

/** Exported in place of tool arguments that never formed valid JSON. */
const INCOMPLETE_TOOL_ARGUMENTS = "<omitted: incomplete tool arguments>";

/** Parse complete tool input; never export an unparsed argument fragment. */
function finalizeToolInput(
  block: StreamProcessingState["contentBlocks"][number],
): void {
  if (block.partialJson === undefined) return;
  try {
    const input: unknown = JSON.parse(block.partialJson);
    block.input = isObjectWithStringKeys(input) ? input : block.partialJson;
  } catch {
    block.input = INCOMPLETE_TOOL_ARGUMENTS;
    block.argumentsIncomplete = true;
  }
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
    if (data.content_block.type === "tool_use") {
      // Accumulate into an owned block, never into the application's events.
      const block = { ...data.content_block };
      state.contentBlocks.push(block);
      if (typeof data.index === "number") {
        state.toolBlocksByIndex.set(data.index, block);
      }
    }
  }

  if (data.type === "content_block_delta" && data.delta?.text) {
    // Accumulate all text into the main outputText string
    state.outputText += data.delta.text;
  }

  if (
    data.type === "content_block_delta" &&
    data.delta?.type === "input_json_delta" &&
    typeof data.delta.partial_json === "string" &&
    typeof data.index === "number"
  ) {
    const block = state.toolBlocksByIndex.get(data.index);
    if (block) {
      block.partialJson = (block.partialJson ?? "") + data.delta.partial_json;
    }
  }

  if (data.type === "content_block_stop" && typeof data.index === "number") {
    const block = state.toolBlocksByIndex.get(data.index);
    if (block) {
      finalizeToolInput(block);
      state.toolBlocksByIndex.delete(data.index);
    }
  }

  if (data.type === "message_delta" && typeof data.delta?.stop_reason === "string") {
    state.finishReason = data.delta.stop_reason;
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
  if (typeof data.stop_reason === "string") state.finishReason = data.stop_reason;

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
    typeof data.completionReason === "string" ||
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
    data.messageStop?.stopReason ||
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
  if (typeof data.completionReason === "string") state.finishReason = data.completionReason;

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
  if (typeof data.messageStop?.stopReason === "string") state.finishReason = data.messageStop.stopReason;

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
  finishReason,
}: {
  span: Span;
  outputText: string;
  contentBlocks: StreamProcessingState["contentBlocks"];
  usage: UsageAttributes;
  finishReason?: string;
}): void {
  const incompleteBlocks = contentBlocks.filter(block => block.argumentsIncomplete);
  if (incompleteBlocks.length > 0) {
    setSpanAttribute(span, "fi.tool_call.arguments_incomplete", true);
    // Sum of UTF-16 code units received for incomplete calls; no argument text.
    setSpanAttribute(span, "fi.tool_call.arguments_received_length",
      incompleteBlocks.reduce((length, block) => length + (block.partialJson?.length ?? 0), 0));
  }
  if (finishReason) {
    setSpanAttribute(span, SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS, [finishReason]);
  }

  // Create the output value structure like the original streaming code
  const toolCalls = contentBlocks
    .filter((block) => block.type === "tool_use")
    .map((block) => ({
      id: block.id || "unknown",
      name: block.name || "unknown",
      input: block.input ?? {},
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
        arguments: block.argumentsIncomplete
          ? INCOMPLETE_TOOL_ARGUMENTS
          : block.partialJson ?? JSON.stringify(block.input || {}),
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
  if (usage.total_tokens !== undefined) {
    setSpanAttribute(
      span,
      SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
      usage.total_tokens,
    );
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

/** Compatibility helper; send instrumentation uses pull-driven observation below. */
export function safelySplitStream({
  originalStream,
}: {
  originalStream: AsyncIterable<unknown>;
}): {
  instrumentationStream?: AsyncIterable<unknown>;
  userStream: AsyncIterable<unknown>;
} {
  try {
    // Create two PassThrough streams in object mode
    const instrumentationStream = new PassThrough({ objectMode: true });
    const userStream = new PassThrough({ objectMode: true });

    // Handle error events even before either consumer starts iterating. Node
    // retains destroy(error) in the stream state, so later iteration still rejects.
    instrumentationStream.on("error", () => {});
    userStream.on("error", () => {});

    // Function to consume the original stream and write to both outputs
    const consumeAndDuplicate = async () => {
      try {
        for await (const chunk of originalStream) {
          // Write the same chunk to both streams
          instrumentationStream.write(chunk);
          userStream.write(chunk);
        }
        // End both streams when original is complete
        instrumentationStream.end();
        userStream.end();
      } catch (error) {
        // Propagate errors to both streams
        instrumentationStream.destroy(error as Error);
        userStream.destroy(error as Error);
      }
    };

    // Start consuming in the background (non-blocking)
    consumeAndDuplicate();

    return {
      instrumentationStream,
      userStream,
    };
  } catch (error) {
    // Fallback: preserve original stream for user, skip instrumentation
    diag.warn(
      "Failed to split stream using PassThrough, falling back to user-only stream:",
      error,
    );
    return {
      userStream: originalStream,
    };
  }
}

interface UnconsumedStreamSpan {
  span: Span;
  consumed: boolean;
  ended: boolean;
}

// Holdings contain only span state, never the source, wrapper, iterator or a
// callback closing over them. The weak token lives with the returned stream and
// its iterators, so abandoning an unconsumed response can actually trigger GC.
const unconsumedStreams = typeof FinalizationRegistry === "undefined"
  ? undefined
  : new FinalizationRegistry<UnconsumedStreamSpan>((state) => {
    if (state.consumed || state.ended) return;
    state.ended = true;
    try {
      try {
        state.span.setAttribute("fi.stream.consumed", false);
        state.span.setStatus({ code: SpanStatusCode.UNSET });
      } finally {
        state.span.end();
      }
    } catch (error) {
      diag.warn("Error finalizing unconsumed Bedrock stream:", error);
    }
  });

/**
 * Observes Bedrock events only when the user requests them. No tee or background
 * reader is created, so an idle/slow consumer cannot queue extra response data.
 * Iterator control methods and upstream failures are forwarded unchanged, while
 * telemetry failures are isolated from the application.
 *
 * gen_ai.server.time_to_first_token is measured in milliseconds from send() until the
 * first chunk the CALLER pulls, including caller delay (not network arrival).
 * Never-iterated streams end best-effort on GC as UNSET with
 * fi.stream.consumed=false; no timer or upstream pull is scheduled.
 */
export function observeBedrockStream({
  stream,
  span,
  modelType,
  onEnd,
  onError,
  startedAt = performance.now(),
}: {
  stream: AsyncIterable<unknown>;
  span: Span;
  modelType: LLMSystem;
  onEnd: (cancelled: boolean) => void;
  onError: (error: unknown) => void;
  startedAt?: number;
}): AsyncIterable<unknown> {
  const state: StreamProcessingState = {
    outputText: "",
    contentBlocks: [],
    toolBlocksByIndex: new Map(),
    rawUsageData: {},
  };
  const lifetime = {};
  const lifecycle: UnconsumedStreamSpan = { span, consumed: false, ended: false };
  unconsumedStreams?.register(lifetime, lifecycle, lifetime);
  let receivedFirstChunk = false;

  const consume = () => {
    if (lifecycle.consumed) return;
    lifecycle.consumed = true;
    unconsumedStreams?.unregister(lifetime);
  };

  // These safety boundaries are synchronous and never wrap upstream operations.
  const processChunk = withSafety({
    fn: (chunk: unknown) => {
      if (lifecycle.ended || !isValidStreamChunk(chunk)) return;
      const text = new TextDecoder().decode(chunk.chunk.bytes);
      const lines = text.split("\n").filter((line) => line.trim());
      for (const line of lines) {
        try {
          const rawData = JSON.parse(line);
          if (!isValidStreamEventData(rawData)) continue;
          const data: StreamEventData = rawData;
          if (modelType === LLMSystem.ANTHROPIC) {
            processAnthropicStreamChunk(data, state);
          } else if (modelType === LLMSystem.AMAZON) {
            if (isTitanStreamChunk(data)) {
              processTitanStreamChunk(data, state);
            } else if (isNovaStreamChunk(data)) {
              processNovaStreamChunk(data, state);
            }
          } else if (modelType === LLMSystem.META) {
            processMetaStreamChunk(data, state);
          }
        } catch (parseError) {
          // Malformed JSON must not prevent delivery of the original event.
          continue;
        }
      }
    },
    onError: (error) => diag.warn("Error observing Bedrock stream chunk:", error),
  });

  const finish = withSafety({
    fn: (notify: () => void) => {
      if (lifecycle.ended) return;
      lifecycle.ended = true;
      unconsumedStreams?.unregister(lifetime);
      try {
        // Preserve complete or partial arguments on completion, failure or cancellation.
        for (const block of state.toolBlocksByIndex.values()) {
          finalizeToolInput(block);
        }
        setStreamingOutputAttributes({
          span,
          outputText: state.outputText,
          contentBlocks: state.contentBlocks,
          usage: normalizeStreamUsageData(state.rawUsageData, modelType),
          finishReason: state.finishReason,
        });
      } finally {
        // End even a cancelled span, including when attribute extraction fails.
        notify();
      }
    },
    onError: (error) => diag.warn("Error finalizing Bedrock stream attributes:", error),
  });

  const observe = async (
    operation: () => Promise<IteratorResult<unknown>>,
    control: "next" | "return" | "throw" = "next",
    thrown?: unknown,
  ): Promise<IteratorResult<unknown>> => {
    if (control === "next") consume();
    try {
      const result = await operation();
      if (!result.done) {
        consume();
        if (!receivedFirstChunk && !lifecycle.ended) {
          receivedFirstChunk = true;
          // Isolate telemetry, including the timing attribute, from delivery.
          try {
            setSpanAttribute(span, SemanticConventions.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN,
              Math.max(0, performance.now() - startedAt));
          } catch (error) { diag.warn("Error recording Bedrock first chunk:", error); }
        }
        processChunk(result.value);
      }
      // A handled throw that yields another value is not a cancellation.
      if (control === "return" || result.done) finish(() => onEnd(control !== "next"));
      return result;
    } catch (error) {
      // The caller's own terminating throw is cancellation, not a provider
      // failure. A different upstream error (including return failure) stays ERROR.
      finish(() => control === "throw" && error === thrown ? onEnd(true) : onError(error));
      throw error;
    }
  };

  return {
    [Symbol.asyncIterator](): AsyncIterableIterator<unknown> {
      let iterator: AsyncIterator<unknown>;
      try {
        iterator = stream[Symbol.asyncIterator]();
      } catch (error) {
        finish(() => onError(error));
        throw error;
      }
      return {
        next: (...args) => observe(() => iterator.next(...args)),
        return: (...args) =>
          observe(
            () => iterator.return
              ? iterator.return(...args)
              : Promise.resolve({ done: true as const, value: args[0] }),
            "return",
          ),
        throw: iterator.throw
          ? (...args) => observe(() => iterator.throw!(...args), "throw", args[0])
          : undefined,
        [Symbol.asyncIterator]() { return this; },
      };
    },
  };
}
