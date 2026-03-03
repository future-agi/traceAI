import { Span, diag } from "@opentelemetry/api";
import { ConverseResponse } from "@aws-sdk/client-bedrock-runtime";
import {
  SemanticConventions,
  MimeType,
} from "@traceai/fi-semantic-conventions";
import { withSafety, safelyJSONStringify } from "@traceai/fi-core";
import { setSpanAttribute, processMessages } from "./attribute-helpers";

/**
 * Extracts response attributes from a Bedrock Converse API response
 * Sets output messages, token usage, and raw output on the span
 *
 * @param params Object containing extraction parameters
 * @param params.span The OpenTelemetry span to set attributes on
 * @param params.response The ConverseResponse from the Bedrock API
 */
export const extractConverseResponseAttributes = withSafety({
  fn: ({
    span,
    response,
  }: {
    span: Span;
    response: ConverseResponse;
  }): void => {
    // Set raw output
    setSpanAttribute(
      span,
      SemanticConventions.OUTPUT_VALUE,
      safelyJSONStringify(response) ?? "",
    );
    setSpanAttribute(span, SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);

    // Extract output messages
    if (response.output?.message) {
      processMessages({
        span,
        messages: [response.output.message],
        baseKey: SemanticConventions.LLM_OUTPUT_MESSAGES,
      });
    }

    // Extract token usage
    if (response.usage) {
      if (response.usage.inputTokens != null) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
          response.usage.inputTokens,
        );
      }
      if (response.usage.outputTokens != null) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
          response.usage.outputTokens,
        );
      }
      if (
        response.usage.inputTokens != null &&
        response.usage.outputTokens != null
      ) {
        setSpanAttribute(
          span,
          SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
          response.usage.inputTokens + response.usage.outputTokens,
        );
      }
    }

    // Extract response model if available
    if (response.metrics) {
      setSpanAttribute(
        span,
        SemanticConventions.RAW_OUTPUT,
        safelyJSONStringify(response) ?? "",
      );
    }
  },
  onError: (error) => {
    diag.warn("Error extracting Converse response attributes:", error);
  },
});
