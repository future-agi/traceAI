import { Attributes, AttributeValue } from "@opentelemetry/api";
import { REDACTED_VALUE } from "./constants";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { MaskingRule, MaskingRuleArgs, TraceConfig } from "./types";
import { redactPiiInValue } from "./piiRedaction";
import { isErrorDetailAttribute } from "./errorDetails";
import { redactMessages } from "./messageRedaction";

// Apply text and image settings together: the masking rule list stops at its
// first match, so independent rules would let image hiding bypass text hiding.
const maskInputContentRule: MaskingRule = {
  condition: ({ config, key }) =>
    (config.hideInputText || config.hideInputImages) &&
    key === SemanticConventions.LLM_INPUT_MESSAGES,
  action: ({ config, value }) => value != null
    ? redactMessages({ value, hideText: config.hideInputText, hideImages: config.hideInputImages }) : undefined,
};

const maskOutputTextRule: MaskingRule = {
  condition: ({ config, key }) =>
    config.hideOutputText && key === SemanticConventions.LLM_OUTPUT_MESSAGES,
  action: ({ value }) => value != null
    ? redactMessages({ value, hideText: true, hideImages: false }) : undefined,
};

/**
 * Masks (removes) embedding vectors.
 */
const maskEmbeddingVectorsRule: MaskingRule = {
  condition: ({ config, key }) =>
    config.hideEmbeddingVectors &&
    key.includes(SemanticConventions.EMBEDDING_EMBEDDINGS) &&
    key.includes(SemanticConventions.EMBEDDING_VECTOR),
  action: () => undefined,
};

/**
 * A list of {@link MaskingRule}s that are applied to span attributes to either redact or remove sensitive information.
 * With JSON blob format, messages are stored as a single JSON string under gen_ai.input.messages / gen_ai.output.messages.
 * The rules now parse and modify the JSON blob content.
 */
const maskingRules: MaskingRule[] = [
  {
    // Error text (exception messages, stack traces, provider error bodies) can quote the content
    condition: ({ config, key }) =>
      config.hideErrorDetails && isErrorDetailAttribute(key),
    action: () => undefined,
  },
  {
    condition: ({ config, key }) =>
      config.hideInputs && key === SemanticConventions.INPUT_VALUE,
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // The raw request body (e.g. the full OpenAI request, messages included), like input.value
    condition: ({ config, key }) =>
      config.hideInputs && key === SemanticConventions.RAW_INPUT,
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // The values filled into a prompt template are the template's inputs
    condition: ({ config, key }) =>
      config.hideInputs && key === SemanticConventions.PROMPT_TEMPLATE_VARIABLES,
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) =>
      config.hideInputs && key === SemanticConventions.INPUT_MIME_TYPE,
    action: () => undefined,
  },
  {
    condition: ({ config, key }) =>
      config.hideOutputs && key === SemanticConventions.OUTPUT_VALUE,
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // The raw response body, like output.value
    condition: ({ config, key }) =>
      config.hideOutputs && key === SemanticConventions.RAW_OUTPUT,
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) =>
      config.hideOutputs && key === SemanticConventions.OUTPUT_MIME_TYPE,
    action: () => undefined,
  },
  {
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideInputMessages) &&
      key === SemanticConventions.LLM_INPUT_MESSAGES,
    action: () => undefined,
  },
  {
    condition: ({ config, key }) =>
      (config.hideOutputs || config.hideOutputMessages) &&
      key === SemanticConventions.LLM_OUTPUT_MESSAGES,
    action: () => undefined,
  },
  maskInputContentRule,
  maskOutputTextRule,
  maskEmbeddingVectorsRule,
];

/**
 * A function that masks (redacts or removes) sensitive information from span attributes based on the trace config.
 * @param config The {@link TraceConfig} to use to determine if the value should be masked
 * @param key The key of the attribute to mask
 * @param value The value of the attribute to mask
 * @returns The redacted value or undefined if the value should be masked, otherwise the original value
 */
export function mask({
  config,
  key,
  value,
}: MaskingRuleArgs): AttributeValue | undefined {
  for (const rule of maskingRules) {
    if (rule.condition({ config, key, value })) {
      value = rule.action({ config, key, value });
      break;
    }
  }
  // Apply PII redaction to retained text even when a content rule matched.
  if (config.piiRedaction && value != null) {
    return redactPiiInValue(value);
  }
  return value;
}

/**
 * Applies {@link mask} to every attribute, dropping the ones it removes. Used for attributes set on
 * a {@link FISpan} and for the attributes passed when a span is created, so both get the same
 * masking (content hiding, error-detail hiding and PII redaction).
 * @param config The {@link TraceConfig} to use
 * @param attributes The attributes to mask
 * @returns A new object with the masked attributes
 */
export function maskAttributes({
  config,
  attributes,
}: {
  config: TraceConfig;
  attributes: Attributes;
}): Attributes {
  const masked: Attributes = {};
  for (const [key, value] of Object.entries(attributes)) {
    const maskedValue = mask({ config, key, value });
    if (maskedValue != null) {
      masked[key] = maskedValue;
    }
  }
  return masked;
}
