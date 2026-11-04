import type { Attributes, AttributeValue } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import {
  ERROR_DETAIL_ATTRIBUTE_KEYS,
  REDACTED_VALUE,
  stripStackTrace,
  redactPiiInValue,
  type TraceConfig,
} from "@traceai/fi-core";

import { SemanticConventions as FI } from "@traceai/fi-semantic-conventions";

// Both native Mastra/GenAI and FI aliases can contain the same content. Filtering
// only input.value/output.value leaves the native copies visible on the wire.
const INPUT_KEYS = new Set<string>([
  FI.INPUT_VALUE, FI.INPUT_MIME_TYPE, FI.RAW_INPUT, FI.LLM_INPUT_MESSAGES,
  FI.PROMPT_TEMPLATE_VARIABLES, "gen_ai.prompt", "gen_ai.system_instructions",
  "gen_ai.tool.call.arguments", "gen_ai.tool.definitions",
  "gen_ai.request.stop_sequences", "gen_ai.tool.description",
]);
const OUTPUT_KEYS = new Set<string>([
  FI.OUTPUT_VALUE, FI.OUTPUT_MIME_TYPE, FI.RAW_OUTPUT, FI.LLM_OUTPUT_MESSAGES,
  "gen_ai.completion", "gen_ai.tool.call.result",
]);
const ERROR_DETAIL = /\.(?:exception\.(?:message|stacktrace)|error\.message|error_message)$/;
const CREDENTIAL = /(?:^|_)(?:api_?key|secret_?key|secret_?access_?key|access_?key_?id|private_?key|client_?secret|access_?token|refresh_?token|id_?token|authorization|password|secret|token|cookie|set_?cookie)$/;

function isCredentialKey(key: string): boolean {
  const normalized = key.replace(/([a-z0-9])([A-Z])/g, "$1_$2").replace(/[.-]/g, "_").toLowerCase();
  return CREDENTIAL.test(normalized);
}

// Also match quoted fields inside non-JSON error text. Consume the complete
// quoted value or Basic/Bearer scheme + payload, not just its first word.
const CREDENTIAL_ASSIGNMENT = /\b((?:proxy[_-]?)?authorization|api[_-]?key|secret[_-]?key|secret[_-]?access[_-]?key|access[_-]?key[_-]?id|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|auth[_-]?token|password|secret|token)(["']?)\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+[^\s,;}"']+|[^\s,;}"']+)/gi;
// A Cookie header is a list, not one scalar token. In unstructured diagnostics
// conservatively consume the complete header line, including every semicolon.
const COOKIE_ASSIGNMENT = /\b((?:set[_-]?)?cookie)(["']?)\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n"']+)/gi;
// Signed Authorization schemes carry multiple comma/space-separated fields.
// A scalar-token matcher would leave AWS Credential/Signature and Digest nonce
// on the wire. Keep a quoted JSON value bounded, or consume the entire line.
const AUTHORIZATION_ASSIGNMENT = /\b((?:proxy[_-]?)?authorization)(["']?)\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n]+)/gi;
const JSON_STRING = /"(?:\\.|[^"\\])*"/g;

/** Scrub JSON objects embedded after a diagnostic prefix, including nested fields. */
function scrubEmbeddedJson(text: string, depth: number): string {
  let result = "";
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== "{" && text[i] !== "[") continue;
    const opening = i;
    const stack: string[] = [];
    let quoted = false;
    let escaped = false;
    let end = i;
    for (; end < text.length; end++) {
      const char = text[end];
      if (quoted) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') quoted = false;
      } else if (char === '"') quoted = true;
      else if (char === "{" || char === "[") stack.push(char);
      else if (char === "}" || char === "]") {
        const expected = char === "}" ? "{" : "[";
        if (stack.pop() !== expected) break;
        if (stack.length === 0) { end++; break; }
      }
    }
    if (stack.length !== 0 || end > text.length) continue;
    const candidate = text.slice(opening, end);
    try {
      const parsed = JSON.parse(candidate);
      const scrubbed = JSON.stringify(redactCredentials(parsed, depth + 1));
      result += text.slice(start, opening) + (scrubbed === JSON.stringify(parsed) ? candidate : scrubbed);
      start = end;
      i = end - 1;
    } catch {
      // This brace-delimited diagnostic was not JSON.
    }
  }
  return result + text.slice(start);
}

/** Mask structured credentials even on older SDKs without a default data filter. */
function redactCredentials(value: unknown, depth = 0): unknown {
  if (depth > 32) return REDACTED_VALUE;
  if (Array.isArray(value)) return value.map(item => redactCredentials(item, depth + 1));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key, isCredentialKey(key) ? REDACTED_VALUE : redactCredentials(item, depth + 1),
    ]));
  }
  if (typeof value !== "string") return value;
  // The native converter serializes object-valued inputs, outputs and metadata.
  const trimmed = value.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[") || trimmed.startsWith('"')) {
    try {
      return JSON.stringify(redactCredentials(JSON.parse(value), depth + 1));
    } catch {
      // Not JSON: handle credential assignments in plain text below.
    }
  }
  // Error prefixes can surround JSON-stringified JSON. Decode complete string
  // literals before inspecting their contents, retaining the original encoding
  // and bounding recursive decoding with the same depth limit as objects.
  const decoded = value.replace(JSON_STRING, literal => {
    try {
      const text = JSON.parse(literal) as string;
      const scrubbed = redactCredentials(text, depth + 1);
      return scrubbed === text ? literal : JSON.stringify(scrubbed);
    } catch {
      return literal;
    }
  });
  return scrubEmbeddedJson(decoded, depth)
    .replace(COOKIE_ASSIGNMENT, `$1$2: "${REDACTED_VALUE}"`)
    .replace(AUTHORIZATION_ASSIGNMENT, `$1$2: "${REDACTED_VALUE}"`)
    .replace(CREDENTIAL_ASSIGNMENT, `$1$2: "${REDACTED_VALUE}"`);
}

function sanitizeValue(value: AttributeValue, config: TraceConfig): AttributeValue {
  const scrubbed = redactCredentials(value) as AttributeValue;
  return config.piiRedaction ? redactPiiInValue(scrubbed) : scrubbed;
}

// Mastra's request-error processors serialize the provider message as the
// top-level `error` string beside messages/retry metadata. This is error detail
// even though it arrives through an input alias. Do not recursively mask user
// messages, drop the whole input, or mutate the native span shared by exporters.
function maskProcessorErrorInput(value: AttributeValue): AttributeValue {
  if (typeof value !== "string") return value;
  try {
    const input: unknown = JSON.parse(value);
    if (input === null || typeof input !== "object" || Array.isArray(input)) return value;
    const payload = input as Record<string, unknown>;
    if (typeof payload.error !== "string") return value;
    return JSON.stringify({ ...payload, error: REDACTED_VALUE });
  } catch {
    return value;
  }
}

function maskAttributes(attributes: Attributes, config: TraceConfig, spanType: AttributeValue | undefined): Attributes {
  const embedding = spanType === "rag_embedding";
  const masked: Attributes = {};
  for (const [key, value] of Object.entries(attributes)) {
    if (value == null) continue;
    if (config.hideErrorDetails && (ERROR_DETAIL_ATTRIBUTE_KEYS.has(key) || ERROR_DETAIL.test(key))) continue;
    const input = INPUT_KEYS.has(key) || /^mastra\..*\.(?:input|instructions)$/.test(key);
    // Observability 1.0 spreads the SDK response onto model_step metadata:
    // body contains the full provider output, alongside id/model/headers. Do not
    // treat all metadata (or a user-defined body on another span) as output.
    const responseBody = spanType === "model_step" && key === "mastra.metadata.body";
    const output = OUTPUT_KEYS.has(key) || /^mastra\..*\.output$/.test(key) || responseBody;
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
    const content = config.hideErrorDetails && spanType === "processor_run" && input
      ? maskProcessorErrorInput(value)
      : value;
    masked[key] = isCredentialKey(key)
      ? REDACTED_VALUE
      : sanitizeValue(content, config);
  }
  return masked;
}

/** Return a sanitized view; never mutate the native span seen by other exporters. */
export function sanitizeSpan(span: ReadableSpan, config: TraceConfig): ReadableSpan {
  const spanType = span.attributes["mastra.span.type"];
  const message = span.status.message;
  const status = message == null || config.hideErrorDetails
    ? { code: span.status.code }
    : { code: span.status.code, message: sanitizeValue(stripStackTrace(message), config) as string };
  return {
    ...span,
    status,
    attributes: maskAttributes(span.attributes, config, spanType),
    events: span.events.map(event => ({
      ...event,
      attributes: maskAttributes(event.attributes ?? {}, config, spanType),
    })),
  };
}
