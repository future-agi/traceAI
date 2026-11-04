import type { Attributes, AttributeValue } from "@opentelemetry/api";
import { REDACTED_VALUE } from "@traceai/fi-core";
import { FISpanKind, MimeType, SemanticConventions } from "@traceai/fi-semantic-conventions";
import {
  FI_SPAN_KIND,
  GEN_AI_SPAN_KIND,
  GEN_AI_TOOL_NAME,
  GENKIT_INPUT_KEYS,
  GENKIT_NEVER_EXPORTED_KEYS,
  GENKIT_OUTPUT_KEYS,
  GENKIT_USAGE_PREFIX,
  GenkitAttributes,
  GenkitSpanType,
  MODEL_NAMESPACE_TO_PROVIDER,
  SUBTYPE_TO_KIND,
  TYPE_TO_KIND,
  USAGE_TO_ATTRIBUTE,
  isPromotedUsageKey,
  splitModelName,
} from "./attributes";
import {
  type ContentPolicy,
  redactEmbeddingVectors,
  redactMessageText,
  SHOW_ALL,
  type ScrubContext,
  scrubContent,
} from "./privacy";

export interface MapOptions {
  /**
   * Resolved TraceConfig content flags (see `resolveContentPolicy`). Default: fi-core's defaults,
   * nothing hidden: `genkit:input` / `genkit:output` are exported and copied to `input.value` /
   * `output.value`.
   */
  policy?: ContentPolicy;
  /** cwd / home directory rewritten in every exported string. Default: none (other local paths are still removed). */
  scrub?: ScrubContext;
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
    // The registered model action name, e.g. "googleai/gemini-3.5-flash" (action.ts:535, 572).
    // The model keys get the vendor id without the plugin namespace, so fi-collector can price
    // the call; the full name stays on genkit:name. Genkit reports no resolved model version.
    const { namespace, model } = splitModelName(name);
    out[SemanticConventions.LLM_MODEL_NAME] = model;
    out[SemanticConventions.GEN_AI_RESPONSE_MODEL] = model;
    const provider = namespace === undefined ? undefined : MODEL_NAMESPACE_TO_PROVIDER[namespace];
    if (provider !== undefined) out[SemanticConventions.LLM_PROVIDER] = provider;
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

/** How one side (input or output) of a span is exported. */
type SideMode = "show" | "text" | "hide";

function sideModes(policy: ContentPolicy, isModelCall: boolean): { input: SideMode; output: SideMode } {
  // On a model span `genkit:input` is the request (the message list) and `genkit:output` the
  // response message, so the *_MESSAGES and *_TEXT flags apply there as they do to
  // gen_ai.input.messages / gen_ai.output.messages in fi-core.
  const input: SideMode =
    policy.hideInputs || (isModelCall && policy.hideInputMessages)
      ? "hide"
      : isModelCall && policy.hideInputText
        ? "text"
        : "show";
  const output: SideMode =
    policy.hideOutputs || (isModelCall && policy.hideOutputMessages)
      ? "hide"
      : isModelCall && policy.hideOutputText
        ? "text"
        : "show";
  return { input, output };
}

function sideValue(
  value: AttributeValue,
  mode: SideMode,
  scrub: ScrubContext,
  hideVectors: boolean,
): AttributeValue | undefined {
  if (mode === "hide") return undefined;
  if (typeof value !== "string") return value;
  let text = mode === "text" ? (redactMessageText(value) ?? REDACTED_VALUE) : value;
  if (hideVectors) text = redactEmbeddingVectors(text) as string;
  return scrubContent(text, scrub);
}

/**
 * Map one Genkit span's attributes to the attributes exported to Future AGI.
 *
 * Returns a new object; `source` is never modified, because Genkit's own
 * exporters (dev UI telemetry server, Firebase plugin) read the same span.
 */
export function mapGenkitAttributes(source: Attributes, options: MapOptions = {}): Attributes {
  const policy = options.policy ?? SHOW_ALL;
  const scrub = options.scrub ?? {};
  const kind = genkitSpanKind(source);
  const isModelCall = kind === FISpanKind.LLM;
  const modes = sideModes(policy, isModelCall);
  // FI_HIDE_EMBEDDING_VECTORS: the vectors are in an embedder span's output.
  const hideVectors = kind === FISpanKind.EMBEDDING && policy.hideEmbeddingVectors === true;
  const out: Attributes = {};

  for (const [key, value] of Object.entries(source)) {
    if (value === undefined) continue;
    if (GENKIT_NEVER_EXPORTED_KEYS.includes(key)) continue;
    if (GENKIT_INPUT_KEYS.includes(key) || GENKIT_OUTPUT_KEYS.includes(key)) {
      const isInput = GENKIT_INPUT_KEYS.includes(key);
      const exported = sideValue(value, isInput ? modes.input : modes.output, scrub, hideVectors && !isInput);
      if (exported !== undefined) out[key] = exported;
      continue;
    }
    if (!isModelCall && isPromotedUsageKey(key)) {
      // Aggregate usage on a non-model span would be summed again by Observe.
      out[GENKIT_USAGE_PREFIX + key] = value;
      continue;
    }
    out[key] = typeof value === "string" ? scrubContent(value, scrub) : value;
  }

  if (kind !== undefined) {
    if (out[FI_SPAN_KIND] === undefined) out[FI_SPAN_KIND] = kind;
    if (out[GEN_AI_SPAN_KIND] === undefined) out[GEN_AI_SPAN_KIND] = kind;
  }

  if (isModelCall) {
    // Read from the source span before any content is hidden: tokens, finish reason and model are
    // not content and stay available with FI_HIDE_OUTPUTS.
    for (const [key, value] of Object.entries(modelCallAttributes(source))) {
      if (value !== undefined && out[key] === undefined) out[key] = value;
    }
  }

  if (kind === FISpanKind.EMBEDDING) {
    // The registered embedder action name, e.g. "googleai/gemini-embedding-001".
    const name = source[GenkitAttributes.NAME];
    if (typeof name === "string" && name.length > 0) {
      const { namespace, model } = splitModelName(name);
      if (out[SemanticConventions.EMBEDDING_MODEL_NAME] === undefined) out[SemanticConventions.EMBEDDING_MODEL_NAME] = model;
      const provider = namespace === undefined ? undefined : MODEL_NAMESPACE_TO_PROVIDER[namespace];
      if (provider !== undefined && out[SemanticConventions.LLM_PROVIDER] === undefined) out[SemanticConventions.LLM_PROVIDER] = provider;
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

  // input.value / output.value, the keys Future AGI shows as a span's input and output. A hidden
  // side gets fi-core's "__REDACTED__" and no mime type, as fi-core's own masking does.
  const input = source[GenkitAttributes.INPUT];
  if (typeof input === "string" && out[SemanticConventions.INPUT_VALUE] === undefined) {
    if (modes.input === "hide") {
      out[SemanticConventions.INPUT_VALUE] = REDACTED_VALUE;
    } else {
      out[SemanticConventions.INPUT_VALUE] = out[GenkitAttributes.INPUT] as string;
      out[SemanticConventions.INPUT_MIME_TYPE] = MimeType.JSON;
    }
  }
  const output = source[GenkitAttributes.OUTPUT];
  if (typeof output === "string" && out[SemanticConventions.OUTPUT_VALUE] === undefined) {
    if (modes.output === "hide") {
      out[SemanticConventions.OUTPUT_VALUE] = REDACTED_VALUE;
    } else {
      out[SemanticConventions.OUTPUT_VALUE] = out[GenkitAttributes.OUTPUT] as string;
      out[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.JSON;
    }
  }

  return out;
}
