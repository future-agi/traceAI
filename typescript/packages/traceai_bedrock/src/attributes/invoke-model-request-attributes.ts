/**
 * Request attribute extraction for AWS Bedrock instrumentation
 *
 * Handles extraction of semantic convention attributes from InvokeModel requests including:
 * - Base model and system attributes
 * - Input message processing
 * - Tool definition processing
 * - Invocation parameters
 */

import { Span, diag } from "@opentelemetry/api";
import {
  SemanticConventions,
  MimeType,
  LLMSystem,
} from "@traceai/fi-semantic-conventions";
import { InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
import {
  withSafety,
  isObjectWithStringKeys,
  safelyJSONStringify,
} from "@traceai/fi-core";
import {
  InvokeModelRequestBody,
  BedrockMessage,
  isTextContent,
  isImageContent,
  isToolUseContent,
} from "../types/bedrock-types";
import { setSpanAttribute, extractModelName } from "./attribute-helpers";
import {
  extractInvocationParameters,
  parseRequestBody,
  extractToolResultBlocks,
  formatImageUrl,
  normalizeRequestContentBlocks,
} from "./invoke-model-helpers";

/**
 * Serializes a BedrockMessage into a plain object for JSON blob format
 */
function serializeBedrockMessage(message: BedrockMessage): Record<string, unknown> {
  const obj: Record<string, unknown> = {};
  if (message.role) obj.role = message.role;

  if (typeof message.content === "string") {
    obj.content = message.content;
  } else if (Array.isArray(message.content)) {
    const textParts: string[] = [];
    const toolCalls: Record<string, unknown>[] = [];

    message.content.forEach((content) => {
      if (isTextContent(content)) {
        textParts.push(content.text);
      } else if (isImageContent(content)) {
        const imageUrl = formatImageUrl(content.source);
        textParts.push(`[image: ${imageUrl}]`);
      } else if (isToolUseContent(content)) {
        toolCalls.push({
          id: content.id,
          function: { name: content.name, arguments: JSON.stringify(content.input) },
        });
      }
    });

    // Check for tool results
    const toolResultBlocks = extractToolResultBlocks(message.content);
    if (toolResultBlocks.length > 0) {
      obj.tool_call_id = toolResultBlocks[0].tool_use_id;
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
 * Extracts base request attributes
 */
function extractBaseRequestAttributes({
  span,
  command,
  requestBody,
  system,
}: {
  span: Span;
  command: InvokeModelCommand;
  requestBody: InvokeModelRequestBody;
  system: LLMSystem;
}): void {
  const modelId = command.input?.modelId || "unknown";
  setSpanAttribute(span, SemanticConventions.LLM_MODEL_NAME, extractModelName(modelId));

  const inputValue = JSON.stringify(requestBody);
  setSpanAttribute(span, SemanticConventions.INPUT_VALUE, inputValue);
  setSpanAttribute(span, SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);

  const invocationParams = extractInvocationParameters(requestBody, system);
  if (Object.keys(invocationParams).length > 0) {
    setSpanAttribute(span, SemanticConventions.LLM_INVOCATION_PARAMETERS, JSON.stringify(invocationParams));
  }
}

/**
 * Extracts input messages as a JSON blob
 */
function extractInputMessagesAttributes({
  span,
  requestBody,
  system,
}: {
  span: Span;
  requestBody: InvokeModelRequestBody;
  system: LLMSystem;
}): void {
  const messages = normalizeRequestContentBlocks(requestBody, system);
  if (messages && Array.isArray(messages)) {
    const serialized = messages.map((msg) => serializeBedrockMessage(msg));
    setSpanAttribute(span, SemanticConventions.LLM_INPUT_MESSAGES, safelyJSONStringify(serialized) ?? "[]");
  }
}

/**
 * Extracts tool definitions as a JSON blob
 */
function extractInputToolAttributes({
  span,
  requestBody,
  system,
}: {
  span: Span;
  requestBody: InvokeModelRequestBody;
  system: LLMSystem;
}): void {
  let tools: unknown[] | undefined;

  if (
    system === LLMSystem.AMAZON &&
    requestBody.toolConfig &&
    isObjectWithStringKeys(requestBody.toolConfig) &&
    requestBody.toolConfig.tools &&
    Array.isArray(requestBody.toolConfig.tools)
  ) {
    tools = requestBody.toolConfig.tools;
  } else if (requestBody.tools && Array.isArray(requestBody.tools)) {
    tools = requestBody.tools;
  }

  if (tools && tools.length > 0) {
    setSpanAttribute(span, SemanticConventions.LLM_TOOLS, safelyJSONStringify(tools) ?? "[]");
  }
}

/**
 * Extracts semantic convention attributes from InvokeModel request command
 * Main entry point for processing InvokeModel API requests with comprehensive error handling
 *
 * Processes:
 * - Request body parsing from multiple formats
 * - Base model and system attributes
 * - Input messages with multi-modal content
 * - Tool definitions
 *
 * @param params Object containing extraction parameters
 * @param params.span The OpenTelemetry span to set attributes on
 * @param params.command The InvokeModelCommand to extract attributes from
 */
export const extractInvokeModelRequestAttributes = withSafety({
  fn: ({
    span,
    command,
    system,
  }: {
    span: Span;
    command: InvokeModelCommand;
    system: LLMSystem;
  }): void => {
    const requestBody = parseRequestBody(command);
    if (!requestBody) {
      return;
    }

    extractBaseRequestAttributes({ span, command, requestBody, system });
    extractInputMessagesAttributes({ span, requestBody, system });
    extractInputToolAttributes({ span, requestBody, system });
  },
  onError: (error) => {
    diag.warn("Error extracting InvokeModel request attributes:", error);
  },
});