import type { Attributes, AttributeValue } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import {
  ERROR_DETAIL_ATTRIBUTE_KEYS,
  REDACTED_VALUE,
  stripStackTrace,
  type TraceConfig,
} from "@traceai/fi-core";

import { SemanticConventions as FI } from "@traceai/fi-semantic-conventions";

// Both native Mastra/GenAI and FI aliases can contain the same content. Filtering
// only input.value/output.value leaves the native copies visible on the wire.
const INPUT_KEYS = new Set<string>([
  FI.INPUT_VALUE, FI.INPUT_MIME_TYPE, FI.RAW_INPUT, FI.LLM_INPUT_MESSAGES,
  FI.PROMPT_TEMPLATE_VARIABLES, "gen_ai.prompt", "gen_ai.system_instructions",
  "gen_ai.tool.call.arguments", "gen_ai.tool.definitions",
]);
const OUTPUT_KEYS = new Set<string>([
  FI.OUTPUT_VALUE, FI.OUTPUT_MIME_TYPE, FI.RAW_OUTPUT, FI.LLM_OUTPUT_MESSAGES,
  "gen_ai.completion", "gen_ai.tool.call.result",
]);
const ERROR_DETAIL = /\.(?:exception\.(?:message|stacktrace)|error\.message|error_message)$/;
const CREDENTIAL = /(?:^|[._-])(?:api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|token|cookie|set[_-]?cookie)$/i;

/** Mask structured credentials even on older SDKs without a default data filter. */
function redactCredentials(value: unknown, depth = 0): unknown {
  if (depth > 32) return REDACTED_VALUE;
  if (Array.isArray(value)) return value.map(item => redactCredentials(item, depth + 1));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key, CREDENTIAL.test(key) ? REDACTED_VALUE : redactCredentials(item, depth + 1),
    ]));
  }
  if (typeof value !== "string") return value;
  // The native converter serializes object-valued inputs, outputs and metadata.
  const trimmed = value.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return JSON.stringify(redactCredentials(JSON.parse(value), depth + 1));
    } catch {
      // Not JSON: handle credential assignments in plain text below.
    }
  }
  return value.replace(
    /\b(api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|token)\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+/gi,
    `$1=${REDACTED_VALUE}`,
  );
}

function maskAttributes(attributes: Attributes, config: TraceConfig, embedding: boolean): Attributes {
  const masked: Attributes = {};
  for (const [key, value] of Object.entries(attributes)) {
    if (value == null) continue;
    if (config.hideErrorDetails && (ERROR_DETAIL_ATTRIBUTE_KEYS.has(key) || ERROR_DETAIL.test(key))) continue;
    const input = INPUT_KEYS.has(key) || /^mastra\..*\.(?:input|instructions)$/.test(key);
    const output = OUTPUT_KEYS.has(key) || /^mastra\..*\.output$/.test(key);
    // Native and FI I/O blobs do not share a reliable typed message schema.
    // Hide the entire alias for granular content flags rather than leave a
    // second unmasked copy (including tool arguments and system instructions).
    const hideInput = input && (config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages);
    const hideOutput = output && (config.hideOutputs || config.hideOutputMessages || config.hideOutputText);
    const hideVector = config.hideEmbeddingVectors && (/(?:embedding|embeddings).*vector/.test(key) || (embedding && output));
    if (hideInput || hideOutput || hideVector) {
      if (!key.endsWith("mime_type")) masked[key] = REDACTED_VALUE;
      continue;
    }
    masked[key] = CREDENTIAL.test(key)
      ? REDACTED_VALUE
      : redactCredentials(value) as AttributeValue;
  }
  return masked;
}

/** Return a sanitized view; never mutate the native span seen by other exporters. */
export function sanitizeSpan(span: ReadableSpan, config: TraceConfig): ReadableSpan {
  const embedding = span.attributes["mastra.span.type"] === "rag_embedding";
  const message = span.status.message;
  const status = message == null || config.hideErrorDetails
    ? { code: span.status.code }
    : { code: span.status.code, message: redactCredentials(stripStackTrace(message)) as string };
  return {
    ...span,
    status,
    attributes: maskAttributes(span.attributes, config, embedding),
    events: span.events.map(event => ({
      ...event,
      attributes: maskAttributes(event.attributes ?? {}, config, embedding),
    })),
  };
}
