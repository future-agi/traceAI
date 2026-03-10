import { Span, diag } from "@opentelemetry/api";
import { InvokeModelResponse } from "@aws-sdk/client-bedrock-runtime";
import {
  SemanticConventions,
  MimeType,
  LLMSystem,
} from "@traceai/fi-semantic-conventions";
import { withSafety, safelyJSONStringify } from "@traceai/fi-core";
import { setSpanAttribute } from "./attribute-helpers";
import {
  normalizeResponseContentBlocks,
  normalizeUsageAttributes,
} from "./invoke-model-helpers";
import {
  BedrockMessage,
  isTextContent,
  isToolUseContent,
} from "../types/bedrock-types";

/**
 * Parses the InvokeModel response body from various formats (Uint8Array, Buffer, string)
 * into a plain object for attribute extraction.
 */
function parseResponseBody(
  response: InvokeModelResponse,
): Record<string, unknown> | null {
  try {
    if (!response.body) return null;

    let bodyString: string;
    if (typeof response.body === "string") {
      bodyString = response.body;
    } else if (Buffer.isBuffer(response.body)) {
      bodyString = response.body.toString("utf8");
    } else if (response.body instanceof Uint8Array) {
      bodyString = new TextDecoder().decode(response.body);
    } else {
      bodyString = String(response.body);
    }

    return JSON.parse(bodyString) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * Serializes a BedrockMessage into the standard output message format
 */
function serializeBedrockMessage(
  message: BedrockMessage,
): Record<string, unknown> {
  const obj: Record<string, unknown> = {};
  if (message.role) obj.role = message.role;

  if (message.content && Array.isArray(message.content)) {
    const textParts: string[] = [];
    const toolCalls: Record<string, unknown>[] = [];

    for (const block of message.content) {
      if (isTextContent(block)) {
        textParts.push(block.text);
      } else if (isToolUseContent(block)) {
        toolCalls.push({
          id: block.id,
          function: {
            name: block.name,
            arguments: JSON.stringify(block.input),
          },
        });
      }
    }

    if (textParts.length > 0) obj.content = textParts.join("\n");
    if (toolCalls.length > 0) obj.tool_calls = toolCalls;
  }

  return obj;
}

/**
 * Extracts response attributes from a Bedrock InvokeModel API response
 * Parses the response body and sets output messages, token usage on the span
 *
 * @param params.span The OpenTelemetry span to set attributes on
 * @param params.response The InvokeModelResponse from the Bedrock API
 * @param params.system The LLM system type for provider-specific parsing
 */
export const extractInvokeModelResponseAttributes = withSafety({
  fn: ({
    span,
    response,
    system,
  }: {
    span: Span;
    response: InvokeModelResponse;
    system: LLMSystem;
  }): void => {
    const responseBody = parseResponseBody(response);
    if (!responseBody) return;

    // Set raw output
    setSpanAttribute(
      span,
      SemanticConventions.OUTPUT_VALUE,
      safelyJSONStringify(responseBody) ?? "",
    );
    setSpanAttribute(span, SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);

    // Extract and set output messages
    const message = normalizeResponseContentBlocks(responseBody, system);
    if (message) {
      const serialized = serializeBedrockMessage(message as BedrockMessage);
      setSpanAttribute(
        span,
        SemanticConventions.LLM_OUTPUT_MESSAGES,
        safelyJSONStringify([serialized]) ?? "[]",
      );
    }

    // Extract and set token usage
    const usage = normalizeUsageAttributes(responseBody, system);
    if (usage) {
      const usageObj = usage as Record<string, unknown>;
      if (usageObj.input_tokens != null) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
          usageObj.input_tokens as number,
        );
      }
      if (usageObj.output_tokens != null) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
          usageObj.output_tokens as number,
        );
      }
      if (usageObj.total_tokens != null) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
          usageObj.total_tokens as number,
        );
      } else if (
        usageObj.input_tokens != null &&
        usageObj.output_tokens != null
      ) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
          (usageObj.input_tokens as number) +
            (usageObj.output_tokens as number),
        );
      }
    }
  },
  onError: (error) => {
    diag.warn("Error extracting InvokeModel response attributes:", error);
  },
});
