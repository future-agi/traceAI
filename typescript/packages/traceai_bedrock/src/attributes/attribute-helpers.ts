import { Span, AttributeValue, Attributes } from "@opentelemetry/api";
import {
  SemanticConventions,
  LLMSystem,
  FISpanKind,
} from "@traceai/fi-semantic-conventions";
import {
  Message,
  SystemContentBlock,
  ContentBlock,
  ConversationRole,
} from "@aws-sdk/client-bedrock-runtime";
import {
  isConverseTextContent,
  isConverseImageContent,
  isConverseToolUseContent,
  isConverseToolResultContent,
} from "../types/bedrock-types";
import { formatImageUrl } from "./invoke-model-helpers";
import { safelyJSONStringify } from "@traceai/fi-core";

/**
 * Sets a span attribute only if the value is not null, undefined, or empty string
 * Provides null-safe attribute setting for OpenTelemetry spans to avoid polluting traces with empty values
 *
 * @param span The OpenTelemetry span to set the attribute on
 * @param key The attribute key following OpenInference semantic conventions
 * @param value The attribute value to set, will be skipped if null/undefined/empty
 */
export function setSpanAttribute(
  span: Span,
  key: string,
  value: AttributeValue | null | undefined,
): void {
  if (value != null && value !== "") {
    span.setAttribute(key, value);
  }
}

/**
 * Extracts vendor-specific system name from Bedrock model ID
 * Maps model IDs to their corresponding AI system providers
 *
 * @param modelId The full Bedrock model identifier (e.g., "anthropic.claude-3-sonnet-20240229-v1:0")
 * @returns {string} The system provider name (e.g., "anthropic", "meta", "mistral") or "bedrock" as fallback
 */
export function getSystemFromModelId(modelId: string): LLMSystem {
  if (modelId.includes("anthropic")) return LLMSystem.ANTHROPIC;
  if (modelId.includes("ai21")) return LLMSystem.AI21;
  if (modelId.includes("amazon")) return LLMSystem.AMAZON;
  if (modelId.includes("cohere")) return LLMSystem.COHERE;
  if (modelId.includes("meta")) return LLMSystem.META;
  if (modelId.includes("mistral")) return LLMSystem.MISTRALAI;
  return LLMSystem.AMAZON;
}

export function setBasicSpanAttributes(span: Span, llm_system: LLMSystem) {
  setSpanAttribute(
    span,
    SemanticConventions.FI_SPAN_KIND,
    FISpanKind.LLM,
  );

  setSpanAttribute(span, SemanticConventions.LLM_PROVIDER, llm_system);
  setSpanAttribute(span, SemanticConventions.GEN_AI_OPERATION_NAME, "chat");
}

/**
 * Aggregates multiple system prompts into a single string
 * Concatenates all text content from system prompts with double newline separation
 *
 * @param systemPrompts Array of system content blocks from Bedrock Converse API
 * @returns {string} Combined system prompt text with proper formatting
 */
export function aggregateSystemPrompts(
  systemPrompts: SystemContentBlock[],
): string {
  return systemPrompts
    .map((prompt) => prompt.text || "")
    .filter(Boolean)
    .join("\n\n");
}

/**
 * Aggregates system prompts with messages into a unified message array
 * System prompts are converted to a single system message at the beginning of the conversation
 *
 * @param systemPrompts Array of system content blocks to convert to system message
 * @param messages Array of conversation messages
 * @returns {Message[]} Combined message array with system prompt as first message if present
 */
export function aggregateMessages(
  systemPrompts: SystemContentBlock[] = [],
  messages: Message[] = [],
): Message[] {
  const aggregated: Message[] = [];

  if (systemPrompts.length > 0) {
    aggregated.push({
      role: "system" as ConversationRole,
      content: [{ text: aggregateSystemPrompts(systemPrompts) }],
    });
  }

  return [...aggregated, ...messages];
}

/**
 * Serializes a message content block to a plain object for JSON blob format
 */
export function serializeMessageContent(
  content: ContentBlock,
): Record<string, unknown> {
  if (isConverseTextContent(content)) {
    return { type: "text", text: content.text };
  } else if (isConverseImageContent(content)) {
    if (content.image.source.bytes) {
      const base64 = Buffer.from(content.image.source.bytes).toString("base64");
      const mimeType = `image/${content.image.format}`;
      return {
        type: "image",
        image_url: formatImageUrl({ type: "base64", data: base64, media_type: mimeType }),
      };
    }
    return { type: "image" };
  } else if (isConverseToolUseContent(content)) {
    return {
      type: "tool_use",
      id: content.toolUse.toolUseId,
      name: content.toolUse.name,
      input: content.toolUse.input,
    };
  } else if (isConverseToolResultContent(content)) {
    return {
      type: "tool_result",
      tool_use_id: content.toolResult.toolUseId,
      content: content.toolResult.content,
    };
  }
  return {};
}

/**
 * Serializes a single Bedrock message to a plain object for JSON blob format
 */
export function serializeMessage(message: Message): Record<string, unknown> {
  const obj: Record<string, unknown> = {};
  if (message.role) {
    obj.role = message.role;
  }
  if (message.content) {
    const textParts: string[] = [];
    const toolCalls: Record<string, unknown>[] = [];

    for (const content of message.content) {
      if (isConverseTextContent(content)) {
        textParts.push(content.text);
      } else if (isConverseToolUseContent(content)) {
        toolCalls.push({
          id: content.toolUse.toolUseId,
          function: { name: content.toolUse.name, arguments: JSON.stringify(content.toolUse.input) },
        });
      } else if (isConverseToolResultContent(content)) {
        obj.tool_call_id = content.toolResult.toolUseId;
      }
    }

    if (textParts.length > 0) {
      obj.content = textParts.join("\n");
    }
    if (toolCalls.length > 0) {
      obj.tool_calls = toolCalls;
    }
  }
  return obj;
}

/**
 * Processes multiple messages and sets a single JSON blob attribute on span
 */
export function processMessages({
  span,
  messages,
  baseKey,
}: {
  span: Span;
  messages: Message[];
  baseKey:
    | typeof SemanticConventions.LLM_INPUT_MESSAGES
    | typeof SemanticConventions.LLM_OUTPUT_MESSAGES;
}): void {
  const serialized = messages.map((msg) => serializeMessage(msg));
  setSpanAttribute(span, baseKey, safelyJSONStringify(serialized) ?? "[]");
}

// --- Legacy functions kept for backward compatibility with invoke-model paths ---

/**
 * @deprecated Use serializeMessageContent instead
 */
export function getAttributesFromMessageContent(
  content: ContentBlock,
): Attributes {
  const attributes: Attributes = {};
  if (isConverseTextContent(content)) {
    attributes[SemanticConventions.MESSAGE_CONTENT_TYPE] = "text";
    attributes[SemanticConventions.MESSAGE_CONTENT_TEXT] = content.text;
  } else if (isConverseImageContent(content)) {
    attributes[SemanticConventions.MESSAGE_CONTENT_TYPE] = "image";
    if (content.image.source.bytes) {
      const base64 = Buffer.from(content.image.source.bytes).toString("base64");
      const mimeType = `image/${content.image.format}`;
      attributes[
        `${SemanticConventions.MESSAGE_CONTENT_IMAGE}.${SemanticConventions.IMAGE_URL}`
      ] = formatImageUrl({ type: "base64", data: base64, media_type: mimeType });
    }
  }
  return attributes;
}

/**
 * @deprecated Use serializeMessage instead
 */
export function getAttributesFromMessage(message: Message): Attributes {
  const attributes: Attributes = {};
  if (message.role) {
    attributes[SemanticConventions.MESSAGE_ROLE] = message.role;
  }
  if (message.content) {
    let toolCallIndex = 0;
    for (const [index, content] of message.content.entries()) {
      const contentAttributes = getAttributesFromMessageContent(content);
      for (const [key, value] of Object.entries(contentAttributes)) {
        attributes[`${SemanticConventions.MESSAGE_CONTENTS}.${index}.${key}`] = value as AttributeValue;
      }
      if (isConverseToolUseContent(content)) {
        const toolCallPrefix = `${SemanticConventions.MESSAGE_TOOL_CALLS}.${toolCallIndex}`;
        attributes[`${toolCallPrefix}.${SemanticConventions.TOOL_CALL_ID}`] = content.toolUse.toolUseId;
        attributes[`${toolCallPrefix}.${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] = content.toolUse.name;
        attributes[`${toolCallPrefix}.${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] = JSON.stringify(content.toolUse.input);
        toolCallIndex++;
      } else if (isConverseToolResultContent(content)) {
        attributes[SemanticConventions.MESSAGE_TOOL_CALL_ID] = content.toolResult.toolUseId;
      }
    }
  }
  return attributes;
}

/**
 * Extracts clean model name from full Bedrock model ID
 * Removes vendor prefix and version suffixes to get the base model name
 *
 * @param modelId The full Bedrock model identifier
 * @returns {string} The cleaned model name (e.g., "claude-3-sonnet" from "anthropic.claude-3-sonnet-20240229-v1:0")
 */
export function extractModelName(modelId: string): string {
  const parts = modelId.split(".");
  if (parts.length > 1) {
    const modelPart = parts[1];
    if (modelId.includes("anthropic")) {
      const versionIndex = modelPart.indexOf("-v");
      if (versionIndex > 0) {
        return modelPart.substring(0, versionIndex);
      }
    }
    return modelPart;
  }
  return modelId;
}