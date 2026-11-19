import { Span, diag } from "@opentelemetry/api";
import { ConverseResponse } from "@aws-sdk/client-bedrock-runtime";
import {
  SemanticConventions,
  MimeType,
} from "@traceai/fi-semantic-conventions";
import {
  withSafety,
  safelyJSONStringify,
  generateTraceConfig,
  type TraceConfigOptions,
} from "@traceai/fi-core";
import { setSpanAttribute, processMessages } from "./attribute-helpers";

/**
 * Extracts response attributes from a Bedrock Converse API response
 * Sets output messages, token usage, and raw output on the span
 *
 * @param params Object containing extraction parameters
 * @param params.span The OpenTelemetry span to set attributes on
 * @param params.response The ConverseResponse from the Bedrock API
 * @param params.traceConfig The trace config in effect; with any hide setting on, only the
 *   response-side fields (output, stop reason, usage, metrics) are written to output.value
 */
/**
 * True while any hide setting is on. The whole response object is not recorded then: it can hold
 * request-side fields (guardrail assessments quote the matched request text).
 */
function anyHideSettingOn(traceConfig?: TraceConfigOptions): boolean {
  const config = generateTraceConfig(traceConfig);
  return Boolean(
    config.hideInputs || config.hideOutputs ||
    config.hideInputMessages || config.hideOutputMessages ||
    config.hideInputText || config.hideOutputText ||
    config.hideInputImages,
  );
}

export const extractConverseResponseAttributes = withSafety({
  fn: ({
    span,
    response,
    traceConfig,
  }: {
    span: Span;
    response: ConverseResponse;
    traceConfig?: TraceConfigOptions;
  }): void => {
    const hidden = anyHideSettingOn(traceConfig);
    const { output, stopReason, usage, metrics } = response;
    setSpanAttribute(
      span,
      SemanticConventions.OUTPUT_VALUE,
      safelyJSONStringify(hidden ? { output, stopReason, usage, metrics } : response) ?? "",
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
    if (response.metrics && !hidden) {
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
