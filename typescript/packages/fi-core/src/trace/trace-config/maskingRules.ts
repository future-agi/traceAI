import { Attributes, AttributeValue } from "@opentelemetry/api";
import { REDACTED_VALUE } from "./constants";
import { SemanticConventions, SemanticAttributePrefixes, RetrievalAttributePostfixes } from "@traceai/fi-semantic-conventions";
import { MaskingRule, MaskingRuleArgs, TraceConfig } from "./types";
import { redactPiiInValue } from "./piiRedaction";
import { isErrorDetailAttribute } from "./errorDetails";
import { redactMessages } from "./messageRedaction";
import { capBase64Images } from "./imageCap";

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
const retrievalDocuments = `${SemanticAttributePrefixes.retrieval}.${RetrievalAttributePostfixes.documents}`;

/**
 * Content keys masked as a whole, by prefix (the key itself or anything below `prefix.`), so that
 * this SDK masks exactly what Python's `TraceConfig.mask` masks
 * (python/fi_instrumentation/instrumentation/config.py: `_INPUT_CONTENT_PREFIXES`,
 * `_OUTPUT_CONTENT_PREFIXES`, `_MIXED_CONTENT_PREFIXES`). Names, ids, counts, model and usage keys
 * are not content and are not listed. The `alias` entries are older names that TypeScript packages
 * write for a field Python spells differently (`SemanticConventions.TOOL_PARAMETERS` is
 * `tool.parameters`, Python's `gen_ai.tool.parameters`).
 *
 * | list   | Python key                                  | here                                   |
 * | ------ | ------------------------------------------- | -------------------------------------- |
 * | input  | gen_ai.system_instructions, gen_ai.request.parameters, gen_ai.request.stop_sequences, gen_ai.tool.definitions, gen_ai.tool.call.arguments, gen_ai.input.images, tool.json_schema, ... | listed below; before this list only llm.tools / llm.invocation_parameters (exact keys), prompt template, input.value and message keys were masked |
 * | input  | gen_ai.tool.parameters                      | listed, plus alias tool.parameters     |
 * | input  | gen_ai.tool.description                     | listed, plus alias tool.description    |
 * | output | gen_ai.tool.call.result, retrieval, reranker output documents, image / guardrail / evaluation / computer-use results | listed below |
 * | either | voice and audio transcripts / urls, computer-use screenshot and current url, gen_ai.tool.call | listed below |
 */
const INPUT_CONTENT_PREFIXES: readonly string[] = [
  "gen_ai.system_instructions",
  "gen_ai.request.parameters",
  "gen_ai.request.stop_sequences",
  "gen_ai.agent.description",
  "gen_ai.image.prompt",
  "gen_ai.image.negative_prompt",
  "gen_ai.input.images",
  "gen_ai.prompts",
  "gen_ai.tool.definitions",
  "gen_ai.tool.description",
  "gen_ai.tool.call.arguments",
  "gen_ai.tool.parameters",
  "tool_call.function.arguments",
  "message.function_call_arguments_json",
  "gen_ai.computer_use.text",
  "gen_ai.computer_use.element_selector",
  "tool.json_schema",
  "gen_ai.retrieval.query",
  "gen_ai.reranker.query",
  "gen_ai.reranker.input_documents",
  "reranker.query",
  "reranker.input_documents",
  // aliases
  "tool.parameters",
  "tool.description",
];
const OUTPUT_CONTENT_PREFIXES: readonly string[] = [
  "gen_ai.tool.call.result",
  "gen_ai.image.revised_prompt",
  "gen_ai.image.output_urls",
  "gen_ai.guardrail.modified_output",
  "gen_ai.evaluation.explanation",
  "gen_ai.computer_use.result",
  "retrieval.documents",
  "gen_ai.retrieval.documents",
  "gen_ai.reranker.output_documents",
  "reranker.output_documents",
];
// Public fields that do not encode a direction and may hold either side.
const MIXED_CONTENT_PREFIXES: readonly string[] = [
  "gen_ai.voice.transcript",
  "gen_ai.voice.recording.url",
  "gen_ai.voice.recording.stereo_url",
  "gen_ai.voice.recording.customer_url",
  "gen_ai.voice.recording.assistant_url",
  "gen_ai.audio.url",
  // gen_ai.audio.transcript is deliberately not listed: the auto-instrumentation wire layer knows
  // whether a transcript is the input (tts) or the output (stt) and masks it by direction, which
  // is stricter than Python's either-flag rule (traceai_auto test/e2e.test.ts, "audio transcript
  // respects its content side").
  "gen_ai.computer_use.screenshot",
  "gen_ai.computer_use.current_url",
];

function hasContentPrefix(key: string, prefixes: readonly string[]): boolean {
  return prefixes.some((prefix) => key === prefix || key.startsWith(`${prefix}.`));
}

const maskingRules: MaskingRule[] = [
  {
    // Vector-query filters are opaque input payloads, not operational metadata.
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideInputText) && key === "db.vector.query.filter",
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // Retrieved documents are outputs too, including metadata that can duplicate
    // private content. Cover both opaque payloads and flattened document fields.
    condition: ({ config, key }) =>
      (config.hideOutputs || config.hideOutputText) &&
      (key === retrievalDocuments || key.startsWith(`${retrievalDocuments}.`)),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // Error text (exception messages, stack traces, provider error bodies) can quote the content
    condition: ({ config, key }) =>
      config.hideErrorDetails && isErrorDetailAttribute(key),
    action: () => undefined,
  },
  {
    // The aggregate tool call can hold both arguments and results: hide it if either side is private.
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideOutputs) && key === SemanticConventions.GEN_AI_TOOL_CALL,
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideOutputs) && hasContentPrefix(key, MIXED_CONTENT_PREFIXES),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) => config.hideInputs && hasContentPrefix(key, INPUT_CONTENT_PREFIXES),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) => config.hideOutputs && hasContentPrefix(key, OUTPUT_CONTENT_PREFIXES),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // Opaque inputs include tool schemas (descriptions, enums, defaults) and
    // their copies in invocation parameters. Do not parse provider-specific
    // JSON: separately recorded model/token metadata remains available.
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideInputText || config.hideInputImages || config.hideInputMessages) &&
      (key === SemanticConventions.INPUT_VALUE || key === SemanticConventions.RAW_INPUT ||
        key === SemanticConventions.LLM_TOOLS || key === SemanticConventions.LLM_INVOCATION_PARAMETERS),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideInputText) &&
      key.startsWith(`${SemanticConventions.EMBEDDING_EMBEDDINGS}.`) &&
      key.endsWith(`.${SemanticConventions.EMBEDDING_TEXT}`),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    // Prompt templates and substitution values are opaque input payloads, including images.
    condition: ({ config, key }) =>
      (config.hideInputs || config.hideInputText || config.hideInputImages || config.hideInputMessages) &&
      (key === SemanticConventions.PROMPT_TEMPLATE_VARIABLES || key === SemanticConventions.PROMPT_TEMPLATE_TEMPLATE),
    action: () => REDACTED_VALUE as AttributeValue,
  },
  {
    condition: ({ config, key }) =>
      config.hideInputs && key === SemanticConventions.INPUT_MIME_TYPE,
    action: () => undefined,
  },
  {
    condition: ({ config, key }) =>
      (config.hideOutputs || config.hideOutputText || config.hideOutputMessages || config.hideEmbeddingVectors) &&
      (key === SemanticConventions.OUTPUT_VALUE || key === SemanticConventions.RAW_OUTPUT ||
        key === SemanticConventions.LLM_FUNCTION_CALL),
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
  if (value == null) {
    return value;
  }
  // Applied to retained text even when a content rule matched
  return redactValue(config, value);
}

/**
 * The value-level redaction every exported value gets, whatever its key: the base64 image cap
 * (`base64ImageMaxLength`, see {@link capBase64Images}), then PII redaction when `piiRedaction`
 * is on. The cap runs first so PII redaction never rewrites part of an oversized image. {@link mask}
 * applies it to span and link attributes; `FISpan` applies it to event attributes, the
 * recorded exception and the ERROR status description.
 */
export function redactValue(config: TraceConfig, value: AttributeValue): AttributeValue {
  const capped = capBase64Images(value, config.base64ImageMaxLength);
  return config.piiRedaction ? redactPiiInValue(capped) : capped;
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
