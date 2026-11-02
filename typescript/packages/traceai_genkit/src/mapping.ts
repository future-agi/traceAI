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
  hidesFreeText,
  isErrorDetailAttribute,
  redactEmbeddingVectors,
  redactMessageText,
  redactMessageImages,
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
  const envelope = parseJson(source[GenkitAttributes.OUTPUT]);
  if (!isRecord(envelope)) {
    return out;
  }
  // Background model actions return an Operation; only its completed output is
  // the model response. Do not promote usage on the surrounding generate span.
  const response = source[GenkitAttributes.SUBTYPE] === "background-model"
    ? envelope.output : envelope;
  if (!isRecord(response)) return out;
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

function sideModes(policy: ContentPolicy, isGeneration: boolean): { input: SideMode; output: SideMode } {
  // Both model actions and the surrounding native generate (util) spans carry the
  // same LLM messages. Mask both copies, without treating tool/flow content as messages.
  const input: SideMode =
    policy.hideInputs || (isGeneration && policy.hideInputMessages)
      ? "hide"
      : isGeneration && policy.hideInputText
        ? "text"
        : "show";
  const output: SideMode =
    policy.hideOutputs || (isGeneration && policy.hideOutputMessages)
      ? "hide"
      : isGeneration && policy.hideOutputText
        ? "text"
        : "show";
  return { input, output };
}

/** Retain classification, never arbitrary provider message/details fields. */
function maskError(record: Record<string, unknown>): void {
  if (record.error === undefined) return;
  const error = record.error;
  record.error = isRecord(error) ? {
    ...(typeof error.status === "string" ? { status: error.status } : {}),
    ...(typeof error.code === "number" && Number.isFinite(error.code) ? { code: error.code } : {}),
  } : {};
}

/** Mutates only JSON.parse's private copy, never an application object or native span. */
function maskGeneration(response: Record<string, unknown>, policy: ContentPolicy, depth: number): void {
  if (policy.hideInputs || policy.hideInputMessages) {
    delete response.request;
  } else if (response.request !== undefined && (policy.hideInputText || policy.hideInputImages)) {
    let request = JSON.stringify(response.request);
    if (policy.hideInputText) request = redactMessageText(request) ?? REDACTED_VALUE;
    if (policy.hideInputImages) request = redactMessageImages(request) ?? REDACTED_VALUE;
    response.request = parseJson(request) ?? REDACTED_VALUE;
  }
  if (hidesFreeText(policy)) {
    delete response.finishMessage;
    maskError(response);
    if (Array.isArray(response.candidates)) {
      for (const candidate of response.candidates) {
        if (isRecord(candidate)) delete candidate.finishMessage;
      }
    }
  }
  if (isRecord(response.operation)) {
    if (depth >= 64) response.operation = REDACTED_VALUE;
    else maskOperation(response.operation, policy, true, depth + 1);
  }
}

/** OperationSchema is used by background-model, check-operation and cancel-operation. */
function maskOperation(operation: Record<string, unknown>, policy: ContentPolicy, model: boolean, depth: number): void {
  if (hidesFreeText(policy)) maskError(operation);
  // Non-model background actions can return arbitrary output; only model operations
  // have a GenerateResponse here. Error policy still covers every Operation.error.
  const isModel = model || (typeof operation.action === "string" && operation.action.startsWith("/background-model/"));
  if (isModel && operation.output !== undefined) {
    if (policy.hideOutputs || policy.hideOutputMessages || depth >= 64) {
      operation.output = REDACTED_VALUE;
    } else if (isRecord(operation.output)) {
      maskGeneration(operation.output, policy, depth + 1);
      if (policy.hideOutputText) operation.output = parseJson(redactMessageText(JSON.stringify(operation.output))) ?? REDACTED_VALUE;
    }
  }
}

/**
 * Generation responses repeat requests and errors, and may wrap an Operation.
 * Poll/cancel actions carry Operation on BOTH sides, including a previous error.
 * Parse only these native envelopes, never arbitrary util/tool/flow content.
 */
function generationOutput(value: AttributeValue, policy: ContentPolicy, operation = false, model = false): AttributeValue {
  if (!hidesFreeText(policy) && !policy.hideInputs && !policy.hideInputMessages &&
      !policy.hideInputText && !policy.hideInputImages &&
      !(operation && (policy.hideOutputs || policy.hideOutputMessages || policy.hideOutputText))) return value;
  const response = parseJson(value);
  // Truncated native envelopes cannot be safely field-masked.
  if (!isRecord(response)) return REDACTED_VALUE;
  if (operation) maskOperation(response, policy, model, 0);
  else maskGeneration(response, policy, 0);
  return JSON.stringify(response);
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
  const isAction = source[GenkitAttributes.TYPE] === GenkitSpanType.ACTION;
  const subtype = isAction ? source[GenkitAttributes.SUBTYPE] : undefined;
  const isBackgroundModel = subtype === "background-model";
  const isOperation = subtype === "check-operation" || subtype === "cancel-operation";
  // Genkit's generateHelper uses the user-supplied stepName as genkit:name.
  // A truncated native request is no longer parseable. On an unknown util span,
  // treat malformed nonempty input as potentially native generation so a response
  // that echoes its prompt fails closed; preserve unrelated parseable util spans.
  const nativeInput = source[GenkitAttributes.INPUT];
  const nativeRequest = parseJson(nativeInput);
  const isNativeGeneration = source[GenkitAttributes.TYPE] === GenkitSpanType.UTIL &&
    ((isRecord(nativeRequest) && typeof nativeRequest.model === "string" && Array.isArray(nativeRequest.messages)) ||
      (typeof nativeInput === "string" && nativeInput.length > 0 && nativeRequest === undefined));
  const isGeneration = isModelCall || isNativeGeneration ||
    (source[GenkitAttributes.TYPE] === GenkitSpanType.UTIL && source[GenkitAttributes.NAME] === "generate") ||
    (subtype === "util" && source[GenkitAttributes.KEY] === "/util/generate");
  const modes = sideModes(policy, isGeneration);
  // FI_HIDE_EMBEDDING_VECTORS: the vectors are in an embedder span's output.
  const hideVectors = kind === FISpanKind.EMBEDDING && policy.hideEmbeddingVectors === true;
  const out: Attributes = {};

  for (const [key, value] of Object.entries(source)) {
    if (value === undefined) continue;
    if (GENKIT_NEVER_EXPORTED_KEYS.includes(key)) continue;
    if (GENKIT_INPUT_KEYS.includes(key) || GENKIT_OUTPUT_KEYS.includes(key)) {
      const isInput = GENKIT_INPUT_KEYS.includes(key);
      const mode = isInput ? modes.input : modes.output;
      let content = value;
      if (mode !== "hide") {
        if (isOperation || (isGeneration && !isInput)) {
          content = generationOutput(content, policy, isOperation || isBackgroundModel, isBackgroundModel);
        } else if (isGeneration && isInput && policy.hideInputImages) {
          content = redactMessageImages(content) ?? REDACTED_VALUE;
        }
      }
      const exported = sideValue(content, mode, scrub, hideVectors && !isInput);
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

  // Native spans bypass FISpan: filter after context merging so those keys cannot reintroduce errors.
  if (hidesFreeText(policy)) {
    for (const key of Object.keys(out)) {
      if (isErrorDetailAttribute(key)) delete out[key];
    }
  }
  return out;
}
