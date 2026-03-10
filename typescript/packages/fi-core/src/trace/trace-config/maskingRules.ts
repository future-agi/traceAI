import { AttributeValue } from "@opentelemetry/api";
import { REDACTED_VALUE } from "./constants";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { MaskingRule, MaskingRuleArgs } from "./types";
import { redactPiiInValue } from "./piiRedaction";

/**
 * Redacts content within a JSON blob of messages.
 * Parses the JSON string, replaces all "content" fields with REDACTED_VALUE,
 * and returns the modified JSON string.
 */
function redactContentInJsonBlob(value: AttributeValue): AttributeValue | undefined {
  if (typeof value !== "string") return value;
  try {
    const messages = JSON.parse(value);
    if (!Array.isArray(messages)) return REDACTED_VALUE;
    const redacted = messages.map((msg: Record<string, unknown>) => {
      const copy = { ...msg };
      if (copy.content !== undefined) {
        copy.content = REDACTED_VALUE;
      }
      return copy;
    });
    return JSON.stringify(redacted);
  } catch {
    return REDACTED_VALUE;
  }
}

/**
 * Redacts images within a JSON blob of messages.
 * Removes image_url fields from content arrays within messages.
 */
function redactImagesInJsonBlob(value: AttributeValue): AttributeValue | undefined {
  if (typeof value !== "string") return value;
  try {
    const messages = JSON.parse(value);
    if (!Array.isArray(messages)) return value;
    const redacted = messages.map((msg: Record<string, unknown>) => {
      const copy = { ...msg };
      if (Array.isArray(copy.content)) {
        copy.content = (copy.content as Record<string, unknown>[]).filter(
          (part) => part.type !== "image_url" && part.type !== "input_image" && part.type !== "image"
        );
      }
      return copy;
    });
    return JSON.stringify(redacted);
  } catch {
    return value;
  }
}

/**
 * Masks input text in LLM input messages (JSON blob format).
 * Redacts "content" fields within the gen_ai.input.messages JSON blob.
 */
const maskInputTextRule: MaskingRule = {
  condition: ({ config, key }) =>
    config.hideInputText &&
    key === SemanticConventions.LLM_INPUT_MESSAGES,
  action: ({ value }: MaskingRuleArgs) => value != null ? redactContentInJsonBlob(value) : undefined,
};

/**
 * Masks output text in LLM output messages (JSON blob format).
 * Redacts "content" fields within the gen_ai.output.messages JSON blob.
 */
const maskOutputTextRule: MaskingRule = {
  condition: ({ config, key }) =>
    config.hideOutputText &&
    key === SemanticConventions.LLM_OUTPUT_MESSAGES,
  action: ({ value }: MaskingRuleArgs) => value != null ? redactContentInJsonBlob(value) : undefined,
};

/**
 * Masks input images in LLM input messages (JSON blob format).
 * Removes image content parts from the gen_ai.input.messages JSON blob.
 */
const maskInputImagesRule: MaskingRule = {
  condition: ({ config, key }) =>
    config.hideInputImages &&
    key === SemanticConventions.LLM_INPUT_MESSAGES,
  action: ({ value }: MaskingRuleArgs) => value != null ? redactImagesInJsonBlob(value) : undefined,
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
    condition: ({ config, key }) =>
      config.hideInputs && key === SemanticConventions.INPUT_VALUE,
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
  maskInputImagesRule,
  maskInputTextRule,
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
      return rule.action({ config, key, value });
    }
  }
  // No key-based rule matched — apply PII redaction if enabled
  if (config.piiRedaction && value != null) {
    return redactPiiInValue(value);
  }
  return value;
}