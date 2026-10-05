import type { Attributes, AttributeValue } from "@opentelemetry/api";
import { FISpanKind, MimeType, SemanticConventions } from "@traceai/fi-semantic-conventions";
import {
  FI_SPAN_KIND,
  GEN_AI_SPAN_KIND,
  GEN_AI_TOOL_NAME,
  GENKIT_CONTENT_KEYS,
  GENKIT_NEVER_EXPORTED_KEYS,
  GENKIT_USAGE_PREFIX,
  GenkitAttributes,
  GenkitSpanType,
  SUBTYPE_TO_KIND,
  TYPE_TO_KIND,
  USAGE_TO_ATTRIBUTE,
  isPromotedUsageKey,
} from "./attributes";

export interface MapOptions {
  /** Copy `genkit:input` / `genkit:output` to `input.value` / `output.value` and keep them. Default false. */
  captureContent?: boolean;
  /**
   * Attributes read from the OTel context when the span started (fi-core
   * `setSession`, `setUser`, `setMetadata`, `setTags`, `setAttributes`). Applied
   * only where the span does not already carry the key.
   */
  contextAttributes?: Attributes;
}

/**
 * Future AGI span kind for a Genkit span, or undefined when the span carries no
 * known `genkit:type` (the span is still exported, just without a kind).
 */
export function genkitSpanKind(attributes: Attributes): FISpanKind | undefined {
  const type = attributes[GenkitAttributes.TYPE];
  if (typeof type !== "string") {
    return undefined;
  }
  if (type === GenkitSpanType.ACTION) {
    const subtype = attributes[GenkitAttributes.SUBTYPE];
    if (typeof subtype === "string" && SUBTYPE_TO_KIND[subtype] !== undefined) {
      return SUBTYPE_TO_KIND[subtype];
    }
    // Every other registered action (custom, prompt, util, indexer, resource, ...).
    return FISpanKind.CHAIN;
  }
  return TYPE_TO_KIND[type];
}

function parseJson(value: AttributeValue | undefined): unknown {
  if (typeof value !== "string" || value.length === 0) {
    return undefined;
  }
  try {
    return JSON.parse(value);
  } catch {
    // Truncated by an attribute length limit, or not JSON. Usage is then unavailable.
    return undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Usage, finish reason and model for one model call, read from the model action span. */
function modelCallAttributes(source: Attributes): Attributes {
  const out: Attributes = {};
  const name = source[GenkitAttributes.NAME];
  if (typeof name === "string" && name.length > 0) {
    // The registered model action name, e.g. "googleai/gemini-2.5-flash" (action.ts:535, 572).
    out[SemanticConventions.LLM_MODEL_NAME] = name;
  }
  const response = parseJson(source[GenkitAttributes.OUTPUT]);
  if (!isRecord(response)) {
    return out;
  }
  const usage = response.usage;
  if (isRecord(usage)) {
    for (const [field, key] of Object.entries(USAGE_TO_ATTRIBUTE)) {
      const value = usage[field];
      if (typeof value === "number" && Number.isFinite(value)) {
        out[key] = value;
      }
    }
  }
  if (typeof response.finishReason === "string") {
    out[SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS] = [response.finishReason];
  }
  return out;
}

/**
 * Map one Genkit span's attributes to the attributes exported to Future AGI.
 *
 * Returns a new object; `source` is never modified, because Genkit's own
 * exporters (dev UI telemetry server, Firebase plugin) read the same span.
 */
export function mapGenkitAttributes(source: Attributes, options: MapOptions = {}): Attributes {
  const captureContent = options.captureContent === true;
  const kind = genkitSpanKind(source);
  const isModelCall = kind === FISpanKind.LLM;
  const out: Attributes = {};

  for (const [key, value] of Object.entries(source)) {
    if (value === undefined) continue;
    if (GENKIT_NEVER_EXPORTED_KEYS.includes(key)) continue;
    if (!captureContent && GENKIT_CONTENT_KEYS.includes(key)) continue;
    if (!isModelCall && isPromotedUsageKey(key)) {
      // Aggregate usage on a non-model span would be summed again by Observe.
      out[GENKIT_USAGE_PREFIX + key] = value;
      continue;
    }
    out[key] = value;
  }

  if (kind !== undefined) {
    if (out[FI_SPAN_KIND] === undefined) out[FI_SPAN_KIND] = kind;
    if (out[GEN_AI_SPAN_KIND] === undefined) out[GEN_AI_SPAN_KIND] = kind;
  }

  if (isModelCall) {
    for (const [key, value] of Object.entries(modelCallAttributes(source))) {
      if (value !== undefined && out[key] === undefined) out[key] = value;
    }
  }

  if (kind === FISpanKind.TOOL) {
    const name = source[GenkitAttributes.NAME];
    if (typeof name === "string" && name.length > 0) {
      if (out[SemanticConventions.TOOL_NAME] === undefined) out[SemanticConventions.TOOL_NAME] = name;
      if (out[GEN_AI_TOOL_NAME] === undefined) out[GEN_AI_TOOL_NAME] = name;
    }
  }

  const agentSession = source[GenkitAttributes.METADATA_AGENT_SESSION_ID];
  if (typeof agentSession === "string" && agentSession.length > 0 && out[SemanticConventions.SESSION_ID] === undefined) {
    out[SemanticConventions.SESSION_ID] = agentSession;
  }

  if (options.contextAttributes) {
    for (const [key, value] of Object.entries(options.contextAttributes)) {
      if (value === undefined || out[key] !== undefined) continue;
      if (isPromotedUsageKey(key) && !isModelCall) {
        out[GENKIT_USAGE_PREFIX + key] = value;
      } else {
        out[key] = value;
      }
    }
  }

  if (captureContent) {
    const input = source[GenkitAttributes.INPUT];
    const output = source[GenkitAttributes.OUTPUT];
    if (typeof input === "string" && out[SemanticConventions.INPUT_VALUE] === undefined) {
      out[SemanticConventions.INPUT_VALUE] = input;
      out[SemanticConventions.INPUT_MIME_TYPE] = MimeType.JSON;
    }
    if (typeof output === "string" && out[SemanticConventions.OUTPUT_VALUE] === undefined) {
      out[SemanticConventions.OUTPUT_VALUE] = output;
      out[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.JSON;
    }
  }

  return out;
}
