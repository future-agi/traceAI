import { Attributes, createContextKey, createTraceState, INVALID_SPAN_CONTEXT, SpanContext, trace } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { generateTraceConfig } from "./trace/trace-config/traceConfig";
import { REDACTED_VALUE } from "./trace/trace-config/constants";
import { stripStackTrace } from "./trace/trace-config/errorDetails";
import { maskAttributes, redactValue } from "./trace/trace-config/maskingRules";
import { capBase64Images } from "./trace/trace-config/imageCap";
import { redactEmbeddingValues, redactRequestMetadata } from "./trace/trace-config/structuredRedaction";
import type { TraceConfigOptions } from "./trace/trace-config/types";
import { DROPPED_CONTENT_COUNT, isProjectionMetadata, PROJECTION_METADATA } from "./attribute-projection";
import { getRegisteredSecrets, registerSecret } from "./secrets";
import { types } from "node:util";
import { hideSemanticContent, isGenkitMappedAlias, carryGenkitAliases } from "./genkit-content-policy";
import type { PrivacyHints } from "./privacy-runtime";
import { completeNonMediaMime, discoverSecrets, indexPrivacyHints, isCredentialName, mediaReplacement, scrubAttribute, scrubValue, providerResponseBinding, scrubProviderResponse, discoverProviderResponseSecrets, rememberProviderResponse, rememberSessionIdentity, isSessionIdentity, s1MediaPrefixReplacement } from "./privacy-runtime";

// Compatibility name for integration tests/packages migrating this round; the agent never reads or propagates it.
export const LIBRARY_SECRETS = createContextKey("traceai.auto.library.credentials");
const HOST_TRUNCATED = "<unknown: host-truncated>";
const HOST_DATA_UNKNOWN = "traceai.capture.host_data_unknown";
// The image cap's whole-value fallback marker (module-private WHOLE_MARKER in
// trace-config/imageCap.ts). It starts with "[" but is not JSON text.
const IMAGE_CAP_WHOLE_MARKER = "[image-redacted]";
const isJsonText = (text: string): boolean => { try { JSON.parse(text); return true; } catch { return false; } };
// Closed keys AND types. No free-form text field is a non-content exemption.
const NON_CONTENT = {
  attributes: {
    "fi.span.kind": ["LLM", "CHAIN", "TOOL", "AGENT", "RETRIEVER", "EMBEDDING", "RERANKER", "GUARDRAIL", "EVALUATOR"],
    "gen_ai.span.kind": ["LLM", "CHAIN", "TOOL", "AGENT", "RETRIEVER", "EMBEDDING", "RERANKER", "GUARDRAIL", "EVALUATOR"],
    "gen_ai.operation.name": ["chat", "text_completion", "embeddings", "create_agent", "invoke_agent", "execute_tool", "generate_content", "retrieval", "text_to_speech", "speech_to_text"],
    "http.response.status_code": "http-status",
    "gen_ai.usage.input_tokens": "count",
    "gen_ai.usage.output_tokens": "count",
    "gen_ai.usage.total_tokens": "count",
    "gen_ai.usage.cache_read_tokens": "count",
    "gen_ai.usage.cache_creation_tokens": "count",
    "gen_ai.usage.input_tokens.audio": "count",
    "gen_ai.usage.output_tokens.audio": "count",
    "gen_ai.usage.output_tokens.reasoning": "count",
    "traceai.wire.truncated": "boolean",
    "traceai.wire.incomplete": "boolean",
    "traceai.wire.body_unavailable": "boolean",
    "traceai.capture.host_data_unknown": "boolean",
    "traceai.capture.dropped_content_count": "count",
  },
  structural: {
    traceId: "lowercase-hex-32", spanId: "lowercase-hex-16", traceFlags: "byte",
    kind: [0, 1, 2, 3, 4], statusCode: [0, 1, 2],
    startTime: "nonnegative-integer-pair", endTime: "nonnegative-integer-pair",
    duration: "nonnegative-integer-pair", eventTime: "nonnegative-integer-pair",
    ended: "boolean", droppedAttributesCount: "count", droppedEventsCount: "count", droppedLinksCount: "count",
  },
} as const;
const NON_CONTENT_KEYS: ReadonlySet<string> = new Set(Object.keys(NON_CONTENT.attributes));
function nonContent(key: string, value: unknown): boolean {
  if (!NON_CONTENT_KEYS.has(key)) return false;
  const rule = NON_CONTENT.attributes[key as keyof typeof NON_CONTENT.attributes];
  if (rule === "boolean") return typeof value === "boolean";
  if (rule === "count") return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
  if (rule === "http-status") return typeof value === "number" && Number.isInteger(value) && value >= 100 && value <= 599;
  return typeof value === "string" && (rule as readonly string[]).includes(value);
}
type InspectionBudget = {
  remaining: number; nodes: number; exhausted: boolean;
  descriptors: WeakMap<object, Map<PropertyKey, PropertyDescriptor | undefined>>;
  entries: WeakMap<object, [string, any][]>;
  refusedEntries: WeakSet<object>;
  suffixes: Map<string, boolean>;
};
const inspectionBudget = (): InspectionBudget => ({ remaining: 4 * 1024 * 1024, nodes: 20000, exhausted: false, descriptors: new WeakMap(), entries: new WeakMap(), refusedEntries: new WeakSet(), suffixes: new Map() });
// Synchronous and stack-restored: reentrant boundaries get their own ledger.
let activeInspection: InspectionBudget | undefined;
function chargeInspection(budget: InspectionBudget, characters = 0, nodes = 1): boolean {
  if (budget.exhausted || characters > budget.remaining || nodes > budget.nodes) {
    budget.exhausted = true; budget.remaining = 0; budget.nodes = 0; return false;
  }
  budget.remaining -= characters; budget.nodes -= nodes; return true;
}
function requireInspection(budget: InspectionBudget, characters = 0, nodes = 1): void {
  if (!chargeInspection(budget, characters, nodes)) throw new Error("Privacy inspection budget");
}
function descriptor(value: object, key: PropertyKey, budget: InspectionBudget): PropertyDescriptor | undefined {
  requireInspection(budget, typeof key === "string" ? key.length : 0);
  let cached = budget.descriptors.get(value);
  if (cached?.has(key)) return cached.get(key);
  // The debit precedes the actual descriptor probe, including rejected accessors.
  const found = Object.getOwnPropertyDescriptor(value, key);
  if (!cached) { cached = new Map(); budget.descriptors.set(value, cached); }
  cached.set(key, found); return found;
}
/** Read data, never accessors, constructors, conversion hooks or proxy traps. */
function data(value: unknown, key: PropertyKey, budget = activeInspection ?? inspectionBudget()): any {
  if (!value || (typeof value !== "object" && typeof value !== "function") || types.isProxy(value)) return undefined;
  let object: object | null = value;
  for (let level = 0; object && level < 16; level++) {
    if (types.isProxy(object)) return undefined;
    const found = descriptor(object, key, budget);
    if (found) return "value" in found ? found.value : undefined;
    requireInspection(budget);
    object = Object.getPrototypeOf(object);
  }
  return undefined;
}
function entries(value: unknown, budget = activeInspection ?? inspectionBudget()): [string, any][] {
  if (!value || typeof value !== "object") return [];
  // An unreadable attribute map is not an empty, successfully inspected map.
  // Do not invoke the proxy's traps, including its ownKeys trap.
  if (types.isProxy(value)) throw new Error("Privacy proxy attribute map");
  requireInspection(budget);
  const cached = budget.entries.get(value);
  if (cached) return cached;
  if (budget.refusedEntries.has(value)) throw new Error("Privacy refused attribute snapshot");
  try {
    // No for-in: inherited keys are not attributes, and hidden/symbol own keys
    // must consume the same allowance as enumerable keys before descriptors run.
    // The primitive's initial allocation is NOT bounded by this consumer ledger;
    // a trusted cardinality-bounded ingress is still needed for a whole-work bound.
    requireInspection(budget);
    const keys = Reflect.ownKeys(value);
    if (!Array.isArray(keys) || types.isProxy(keys)) throw new Error("Privacy own-key snapshot shape");
    const length = data(keys, "length", budget);
    if (!Number.isSafeInteger(length) || length < 0) throw new Error("Privacy own-key snapshot length");
    requireInspection(budget, 0, length);
    const seen = new Set<PropertyKey>();
    const result: [string, any][] = [];
    for (let inspected = 0; inspected < length; inspected++) {
      if (inspected >= 4096) throw new Error("Privacy inspected-key budget");
      const slot = descriptor(keys, String(inspected), budget);
      if (!slot || !("value" in slot)) throw new Error("Privacy own-key snapshot slot");
      const key: unknown = slot.value;
      if ((typeof key !== "string" && typeof key !== "symbol") || seen.has(key)) throw new Error("Privacy own-key snapshot element");
      seen.add(key);
      const found = descriptor(value, key, budget);
      if (!found) throw new Error("Privacy own-key snapshot descriptor");
      if (typeof key === "string" && found.enumerable && "value" in found) result.push([key, found.value]);
    }
    budget.entries.set(value, result); return result;
  } catch (error) {
    // Loss inspection and discovery share this refusal, not a fresh enumeration.
    budget.refusedEntries.add(value); throw error;
  }
}
function truncatedValue(value: unknown, limit: number, budget = activeInspection ?? inspectionBudget(), depth = 0, reuseSuffix = false): boolean {
  requireInspection(budget);
  if (depth > 32) throw new Error("Privacy loss depth");
  if (value === HOST_TRUNCATED) return true;
  if (typeof value === "string") {
    if (value.length >= limit) return true;
    // Initial inspection charges every surface occurrence, even aliased maps.
    // Output construction may reuse that exact immutable string's suffix fact;
    // it must not repeat a probe already paid for by this export's ledger.
    if (reuseSuffix && budget.suffixes.has(value)) return budget.suffixes.get(value)!;
    requireInspection(budget, value.length, 0);
    const truncated = /(?:<truncated>|\[truncated\])\s*$/i.test(value);
    budget.suffixes.set(value, truncated);
    return truncated;
  }
  if (!Array.isArray(value) || types.isProxy(value)) return false;
  const length = data(value, "length", budget);
  if (length > 16384 || length > budget.nodes) throw new Error("Privacy array budget");
  for (let i = 0; i < length; i++) if (truncatedValue(data(value, String(i), budget), limit, budget, depth + 1, reuseSuffix)) return true;
  return false;
}
// Q8b: no prefix, length, hash, digest or other content-dependent marker choice.
function withheldMarker(key: string): string { return key === "session.id" ? "<redacted>" : HOST_TRUNCATED; }

// Only our pre-admission path can attest to a complete text MIME before SDK
// truncation/projection. Keep evidence out of telemetry and invalidate it if any
// processor changes the admitted value. Host-owned exporters are never mutated.
const admittedText = new WeakMap<object, Map<string, unknown>>();
// Markers synthesized AFTER native admission can exceed its value limit. Trust
// only this exact detached map/key/value, never a marker's spelling in telemetry.
const exportedMarkers = new WeakMap<object, Map<string, string>>();
function exportedMarker(attrs: unknown, key: string, value: unknown): boolean {
  return !!attrs && typeof attrs === "object" && typeof value === "string"
    && exportedMarkers.get(attrs)?.get(key) === value;
}
function carryAttributeEvidence(source: unknown, target: Attributes): void {
  if (!source || typeof source !== "object" || types.isProxy(source)) return;
  const proof = admittedText.get(source), next = new Map<string, unknown>(), markers = new Map<string, string>();
  for (const [key, value] of entries(target)) {
    const original = data(source, key);
    if (proof?.has(key) && sameAttribute(original, proof.get(key)) && sameAttribute(original, value)) {
      next.set(key, Array.isArray(value) ? Array.from({ length: data(value, "length") }, (_, i) => data(value, String(i))) : value);
    }
    if (typeof value === "string" && (exportedMarker(source, key, value)
        || value !== original && value.length <= 48 && /^(?:<redacted>|__REDACTED__|\[REDACTED\]|<omitted: (?:[0-9]+ base64 chars|media|data URI)>)$/.test(value))) markers.set(key, value);
  }
  if (next.size) admittedText.set(target, next);
  if (markers.size) exportedMarkers.set(target, markers);
}
// No content/native objects are retained by the cells. All identities are weak.
// Existing detached views share future observations, not a one-time copied bit.
const LOSS_REGISTRY = Symbol.for("traceai.auto.destination.loss.v2");
type Lineage = { parent?: Lineage; limit: number; hostLoss: boolean; uncertain: boolean; authored: number; autoAuthored?: boolean };
type LossRegistry = { version: 2; cells: WeakMap<object, Lineage> };
const lossHost = globalThis as typeof globalThis & { [LOSS_REGISTRY]?: LossRegistry };
if (!lossHost[LOSS_REGISTRY]) Object.defineProperty(lossHost, LOSS_REGISTRY, { value: Object.freeze({ version: 2, cells: new WeakMap<object, Lineage>() }) });
const lossRegistry = lossHost[LOSS_REGISTRY]!;
if (lossRegistry.version !== 2 || !(lossRegistry.cells instanceof WeakMap)) throw new Error("Unsupported auto loss protocol");
function lossContext(value: unknown): object | undefined { return value && typeof value === "object" && !types.isProxy(value) ? value : undefined; }
// Standalone keeps a legacy combined droppedAttributesCount. Its FI component
// is trusted only on the actual object FI wrote (or a detached view FI created),
// never from telemetry, a trace/span ID, or a caller's context-sharing copy.
const PROJECTION_DROPS = Symbol.for("traceai.fi.projection.drops.v1");
type ProjectionDrops = { version: 1; counts: WeakMap<object, number> };
const projectionHost = globalThis as typeof globalThis & { [PROJECTION_DROPS]?: ProjectionDrops };
if (!projectionHost[PROJECTION_DROPS]) Object.defineProperty(projectionHost, PROJECTION_DROPS, { value: Object.freeze({ version: 1, counts: new WeakMap<object, number>() }) });
const projectionDrops = projectionHost[PROJECTION_DROPS]!;
if (projectionDrops.version !== 1 || !(projectionDrops.counts instanceof WeakMap)) throw new Error("Unsupported FI projection protocol");
/** @internal Record only the FI contribution actually added to this object's
 * combined count. This does not mark auto spans or retain any attribute data. */
export function rememberProjectedAttributeDrops(span: object, count: number): void {
  if (!lossContext(span)) return;
  const valid = hostCount(count);
  if (valid > 0) projectionDrops.counts.set(span, valid);
  else projectionDrops.counts.delete(span);
}
function projectedAttributeDrops(surface: unknown): number {
  const key = lossContext(surface);
  return key ? hostCount(projectionDrops.counts.get(key)) : 0;
}
function hostAttributeDrops(surface: unknown, budget: InspectionBudget): number {
  const total = Math.max(hostCount(data(surface, "_droppedAttributesCount", budget)), hostCount(data(surface, "droppedAttributesCount", budget)));
  return Math.max(0, total - projectedAttributeDrops(surface));
}
function root(cell: Lineage): Lineage {
  let current = cell;
  while (current.parent) current = current.parent;
  while (cell.parent) { const next = cell.parent; cell.parent = current; cell = next; }
  return current;
}
function bindLineage(cell: Lineage, identity: unknown): Lineage {
  const key = lossContext(identity); let current = root(cell);
  if (!key) return current;
  const known = lossRegistry.cells.get(key);
  if (known) {
    const other = root(known);
    if (other !== current) {
      other.limit = Math.min(current.limit, other.limit);
      other.hostLoss ||= current.hostLoss; other.uncertain ||= current.uncertain;
      other.authored = Math.max(current.authored, other.authored);
      other.autoAuthored ||= current.autoAuthored === true;
      current.parent = other; current = other;
    }
  }
  lossRegistry.cells.set(key, current); return current;
}
function lineage(span: unknown): Lineage {
  const key = lossContext(span), known = key && lossRegistry.cells.get(key);
  const cell = known ? root(known) : { limit: Infinity, hostLoss: false, uncertain: false, authored: 0 };
  if (key) lossRegistry.cells.set(key, cell);
  return cell;
}
/** @internal Final FI write failure: retain only a refusal bit on the native context. */
export function rememberSpanPrivacyFailure(context: object): void {
  root(lineage(context)).uncertain = true;
}
/** @internal Auto-only FI write accounting. Counts caller-supplied, non-null
 * attribute values that FI admission withheld or changed before the native
 * write. Only that number is kept on the native context's lineage and emitted
 * as dropped_content_count on the detached view; no key or value is retained
 * and native storage is never read, written or replayed. */
export function rememberAuthoredRedactions(context: object, original: Attributes, admitted: Attributes): void {
  // Called only by the auto FI write path, including zero-difference starts.
  // Provenance cannot depend on a positive count or caller telemetry.
  const state = root(lineage(context));
  state.autoAuthored = true;
  const budget = inspectionBudget();
  // Admission spells null/undefined/non-finite numbers as text. That is a
  // representation change, not withheld content.
  const same = (a: unknown, b: unknown): boolean => a === b || (a == null || typeof a === "number") && String(a) === b;
  let count = 0;
  for (const [key, value] of entries(original, budget)) {
    if (value == null) continue;
    const slot = descriptor(admitted, key, budget), kept: unknown = slot && "value" in slot ? slot.value : undefined;
    if (Array.isArray(value) && !types.isProxy(value)) {
      const length = data(value, "length", budget);
      let complete = Array.isArray(kept) && !types.isProxy(kept) && length === data(kept, "length", budget);
      for (let i = 0; complete && i < length; i++) complete = same(data(value, String(i), budget), data(kept, String(i), budget));
      if (!complete) count++;
    } else if (!same(value, kept)) count++;
  }
  if (count === 0) return;
  const current = root(state);
  current.authored = Math.min(Number.MAX_SAFE_INTEGER, current.authored + count);
}
function hostCount(value: unknown): number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0; }
function readResource(resource: unknown, budget: InspectionBudget): unknown {
  const attributes = data(resource, "attributes", budget) ?? data(resource, "_attributes", budget) ?? data(resource, "_memoizedAttributes", budget);
  if (attributes !== undefined) return attributes;
  const pairs = data(resource, "_rawAttributes", budget), result: Attributes = Object.create(null);
  if (!Array.isArray(pairs) || types.isProxy(pairs)) return result;
  const length = data(pairs, "length", budget);
  if (length > 4096) throw new Error("Privacy resource budget");
  for (let i = 0; i < length; i++) {
    const pair = data(pairs, String(i), budget), key = data(pair, "0", budget), value = data(pair, "1", budget);
    if (typeof key !== "string" || value == null) continue;
    requireInspection(budget, key.length);
    if (!Object.prototype.hasOwnProperty.call(result, key)) result[key] = value;
  }
  return result;
}
function surfaceArray(value: unknown, budget: InspectionBudget): any[] {
  if (!Array.isArray(value) || types.isProxy(value)) return [];
  const length = data(value, "length", budget);
  if (length > 4096) throw new Error("Privacy surface budget");
  return Array.from({ length }, (_, i) => data(value, String(i), budget));
}
type LossFacts = { loss: boolean; truncation: boolean; exhausted: boolean };
function inspectAttributeLoss(span: object, valueLimit: number, budget: InspectionBudget, resourceAttributes?: unknown, extraResource?: unknown, fiRefusal = false, knownHostLoss = false): LossFacts {
  const facts: LossFacts = { loss: knownHostLoss, truncation: false, exhausted: false };
  const inspect = (surface: unknown, attributes = data(surface, "attributes", budget)): void => {
    if (hostAttributeDrops(surface, budget) > 0) facts.loss = true;
    for (const [key, value] of entries(attributes, budget)) {
      requireInspection(budget, key.length);
      // FI projection counters record complete omissions, not host loss.
      if ((key === "traceai.wire.truncated" || key === "traceai.wire.incomplete" || key === "traceai.wire.body_unavailable" || key === HOST_DATA_UNKNOWN) && value === true) facts.loss = true;
      // Once blanket loss is known, no content probe can improve the verdict.
      if (!facts.loss && !facts.truncation && !exportedMarker(attributes, key, value) && truncatedValue(value, valueLimit, budget)) facts.truncation = true;
    }
  };
  try {
    for (const name of ["_droppedEventsCount", "droppedEventsCount", "_droppedLinksCount", "droppedLinksCount"]) if (hostCount(data(span, name, budget)) > 0) facts.loss = true;
    const resource = data(span, "resource", budget), scope = data(span, "instrumentationScope", budget) ?? data(span, "instrumentationLibrary", budget);
    inspect(span); inspect(scope); inspect(resource, resourceAttributes ?? readResource(resource, budget)); inspect(undefined, extraResource);
    if (!facts.loss && !facts.truncation && truncatedValue(data(data(span, "status", budget), "message", budget), valueLimit, budget)) facts.truncation = true;
    // Names are not SDK attribute-limit surfaces, but explicit loss markers count.
    for (const value of [data(span, "name", budget), data(scope, "name", budget), data(scope, "version", budget), data(scope, "schemaUrl", budget)]) {
      // A prior FI minimal view writes this constant into names. Re-exporting
      // that refusal must not manufacture host-loss evidence for its lineage.
      if (fiRefusal && value === HOST_TRUNCATED) continue;
      if (!facts.loss && !facts.truncation && truncatedValue(value, Infinity, budget)) facts.truncation = true;
    }
    for (const name of ["events", "links"]) for (const item of surfaceArray(data(span, name, budget), budget)) {
      inspect(item);
      if (!facts.loss && !facts.truncation && name === "events" && truncatedValue(data(item, "name", budget), Infinity, budget)) facts.truncation = true;
    }
  } catch { facts.exhausted = true; }
  facts.loss ||= facts.truncation; return facts;
}
function retainLoss(cell: Lineage, limit: number, facts: LossFacts): void {
  const state = root(cell);
  if (Number.isFinite(limit) && limit >= 0) state.limit = Math.min(state.limit, limit);
  state.hostLoss ||= facts.loss; state.uncertain ||= facts.exhausted;
}
/** @internal Observe native onStart/onEnd without mutating native storage. */
export function rememberSpanValueLimit(span: object): void {
  const previous = activeInspection, budget = inspectionBudget(); activeInspection = budget;
  let cell = lineage(span);
  try {
    cell = bindLineage(cell, data(span, "_spanContext", budget));
    const nativeLimit = data(span, "_attributeValueLengthLimit", budget) ?? data(data(span, "_spanLimits", budget), "attributeValueLengthLimit", budget);
    const limit = Math.min(typeof nativeLimit === "number" && nativeLimit >= 0 ? nativeLimit : Infinity, root(cell).limit);
    retainLoss(cell, limit, inspectAttributeLoss(span, limit, budget, undefined, undefined, root(cell).uncertain, root(cell).hostLoss));
  } catch { root(cell).uncertain = true; }
  finally { activeInspection = previous; }
}
function globalValueLimit(): unknown {
  // Manual mapping processors need not call the agent's onStart hook. Genkit's
  // native spans use the global SDK; its normalized config is a conservative
  // fallback for copies lacking source evidence. Never call provider accessors.
  if (activeInspection) requireInspection(activeInspection);
  let provider: unknown = trace.getTracerProvider();
  for (let depth = 0; provider && depth < 16; depth++) {
    // Newer SDKs retain normalized limits directly, rather than an entire
    // _config object. Older SDKs keep span/general limits inside _config.
    const config = data(provider, "_config");
    const limit = data(data(provider, "_spanLimits"), "attributeValueLengthLimit")
      ?? data(data(config, "spanLimits"), "attributeValueLengthLimit")
      ?? data(data(config, "generalLimits"), "attributeValueLengthLimit");
    if (typeof limit === "number" && limit >= 0) return limit;
    const delegate = data(provider, "_delegate");
    if (!delegate || delegate === provider) break;
    provider = delegate;
  }
  // Some SDK releases keep normalized limits on their tracer rather than
  // the provider. Use that same public accessor as pre-admission projection;
  // the detached export object itself need not expose any SDK slots.
  try {
    if (activeInspection) requireInspection(activeInspection);
    const provider = trace.getTracerProvider();
    const getTracer = data(provider, "getTracer");
    if (typeof getTracer === "function") {
      if (activeInspection) requireInspection(activeInspection);
      const tracer = Reflect.apply(getTracer, provider, ["@traceai/fi-core/privacy-limits"]);
      const getLimits = data(tracer, "getSpanLimits");
      if (activeInspection) requireInspection(activeInspection);
      const limits = typeof getLimits === "function" ? Reflect.apply(getLimits, tracer, []) : data(tracer, "_spanLimits");
      const limit = data(limits, "attributeValueLengthLimit");
      if (typeof limit === "number" && limit >= 0) return limit;
    }
  } catch { /* No attestation: the ordinary conservative copy policy remains. */ }
  return undefined;
}
function sameAttribute(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b) && !types.isProxy(a) && !types.isProxy(b)) {
    const length = data(a, "length");
    return length === data(b, "length") && length <= 16384 && Array.from({ length }, (_, i) => data(a, String(i)) === data(b, String(i))).every(Boolean);
  }
  return a === b;
}
/** @internal Call only with the complete, already-sanitized pre-SDK view. */
export function rememberMediaAdmission(span: object, complete: Attributes): void {
  try {
    const current = data(span, "attributes");
    if (!current || types.isProxy(current)) return;
    const hints = indexPrivacyHints(entries(complete)), proven = new Map<string, unknown>();
    for (const [key, original] of entries(complete)) {
      if (!completeNonMediaMime(key, hints)) continue;
      const actual = data(current, key);
      // SDK strings may be prefixes. Never accept another processor's different
      // value, or an SDK-dropped key, as evidence of complete admission.
      if (typeof original === "string" && typeof actual === "string" && original.startsWith(actual)) proven.set(key, actual);
      else if (sameAttribute(original, actual)) proven.set(key, Array.isArray(actual) ? Array.from({ length: data(actual, "length") }, (_, i) => data(actual, String(i))) : actual);
    }
    admittedText.set(current, proven);
  } catch { /* Missing evidence invokes the conservative export fallback. */ }
}
export type PrivacyOptions = TraceConfigOptions & { hideLLMInvocationParameters?: boolean };

/** The sole FI export boundary. No original span, resource or context is reachable from its snapshot. */
export function privacyFilter(options: PrivacyOptions = {}, configured: readonly string[] = [],
  credentialAttributeSentinel: "<redacted>" | typeof REDACTED_VALUE = "<redacted>") {
  for (const secret of configured) registerSecret(secret);
  // FI configuration is independently authoritative even after transport churn
  // evicts its initial seeds. Cache the merged view, not another secret registry.
  const fixed = configured.filter(value => value.length >= 8);
  let lastRegistry: readonly string[] | undefined, lastSnapshot: readonly string[] = [];
  const snapshot = () => {
    const current = getRegisteredSecrets();
    if (current !== lastRegistry) {
      lastSnapshot = fixed.length ? [...new Set([...current, ...fixed])].sort((a, b) => b.length - a.length) : current;
      lastRegistry = current;
    }
    return lastSnapshot;
  };
  const invocation = options.hideLLMInvocationParameters ?? process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS?.trim().toLowerCase() === "true";
  const resolved = generateTraceConfig(options);
  // Derive this from resolved policy, not only the explicit options. Environment
  // image hiding must protect status/exception details identically.
  const explicitErrors = options.hideErrorDetails !== undefined
    || /^(?:true|false)$/i.test(process.env.FI_HIDE_ERROR_DETAILS?.trim() ?? "");
  const tc = generateTraceConfig({ ...resolved, ...(invocation && !explicitErrors ? { hideErrorDetails: true } : {}) });
  const hideInput = tc.hideInputs || tc.hideInputMessages || tc.hideInputText;
  const hideOutput = tc.hideOutputs || tc.hideOutputMessages || tc.hideOutputText;
  let spanBudget: { remaining: number; nodes: number; unknown: boolean } | undefined;
  let spanScanBudget: InspectionBudget | undefined;
  const attributes = (attrs: unknown, requestSecrets?: readonly string[], operation?: unknown, capsuleDepth = 0, admission = false, incomplete = false, valueLimit = Infinity, unattestedCopy = false, spanType?: unknown, responseContext?: object, outputResponseContext?: object, withholdContent = false, authoredHints?: PrivacyHints, genkit = false): Attributes => {
    if (capsuleDepth > 16) throw new Error("Privacy capsule budget");
    const secrets = requestSecrets ?? snapshot();
    const budget = spanBudget ?? { remaining: 8 * 1024 * 1024, nodes: 20000 };
    const scanBudget = spanScanBudget ?? inspectionBudget();
    const vectorPolicy = (clean: string): string => tc.hideEmbeddingVectors ? redactEmbeddingValues(clean) : clean;
    const string = (value: string): string => {
      if ((budget.remaining -= value.length) < 0) throw new Error("Privacy text budget");
      // Names and non-structural scalar surfaces enter the same value policy.
      const redacted = redactValue(tc, value) as string;
      return vectorPolicy(redacted === REDACTED_VALUE ? redacted : scrubAttribute(redacted, secrets, unattestedCopy));
    };
    if (!admission && withholdContent) {
      const withheld: Attributes = Object.create(null);
      for (const [key, item] of entries(attrs, scanBudget)) {
        if (item === undefined) continue;
        const name = string(key);
        if (!nonContent(key, item)) withheld[name] = withheldMarker(key);
        else {
          // Typed enums/counters are not an exact-secret exemption.
          const raw = String(item), clean = string(raw);
          withheld[name] = clean === raw ? item : clean;
        }
      }
      return withheld;
    }
    // Only discriminator values are examined by the hint index. Do not copy or
    // scan arbitrary withheld payload arrays merely to classify their siblings.
    // Structural output work stays bounded separately; content probes consume
    // the same inspection allowance as loss/discovery/marker selection.
    let unknownHints = false;
    const hintItems = entries(attrs).map(([key, item]): [string, unknown] => {
      if ((budget.remaining -= key.length) < 0 || --budget.nodes < 0) throw new Error("Privacy hint budget");
      const leaf = key.split(/[.\[\]]+/).filter(part => part && !/^\d+$/.test(part)).at(-1);
      const discriminator = indexPrivacyHints([[key, undefined]]).size > 0 || ["type", "modality"].includes(leaf?.replace(/[-_]/g, "").toLowerCase() ?? "");
      if (!discriminator) return [key, undefined];
      const hintValue = (value: unknown): unknown => {
        if (--budget.nodes < 0) throw new Error("Privacy hint budget");
        if (typeof value !== "string") return undefined;
        if (!admission && !chargeInspection(scanBudget, value.length)) { unknownHints = true; return undefined; }
        if ((budget.remaining -= value.length) < 0) throw new Error("Privacy hint budget");
        return value;
      };
      if (Array.isArray(item) && !types.isProxy(item)) {
        const length = data(item, "length");
        if (length > budget.nodes) throw new Error("Privacy array budget");
        return [key, Array.from({ length }, (_, i) => hintValue(data(item, String(i))))];
      }
      return [key, hintValue(item)];
    });
    // A projected map is not a complete sibling set. Its own MIME text cannot
    // grant completeness; unchanged FI pre-admission evidence below still can.
    const hints = indexPrivacyHints(hintItems, "", authoredHints, !admission && incomplete ? 0 : valueLimit);
    const proven = attrs && typeof attrs === "object" ? admittedText.get(attrs) : undefined;
    const value = (item: unknown, key: string, depth = 0, admitted = false): any => {
      if (--budget.nodes < 0 || depth > 32) throw new Error("Privacy value budget");
      // Protected identity/usage is exempt only from the arbitrary-base64
      // heuristic, never from catalog paths/magic or a possibly truncated value.
      admitted ||= !!proven?.has(key) && sameAttribute(item, proven.get(key));
      const truncated = !NON_CONTENT_KEYS.has(key) && typeof item === "string" && item.length >= valueLimit;
      const lostContext = incomplete && !admitted;
      const ambiguousBase64 = !NON_CONTENT_KEYS.has(key) && (truncated || !isProjectionMetadata(key.replace(/(?:\.\d+)+$/, "")));
      const mediaValue = (v: unknown) => mediaReplacement(v, key, hints, true, false, false, lostContext, ambiguousBase64);
      const sessionIdentity = key === "session.id" && isSessionIdentity(responseContext, item);
      if (!admission && !sessionIdentity && isCredentialName(key)) return credentialAttributeSentinel;
      if (tc.hideEmbeddingVectors && /(?:^|\.)(?:embedding|embeddings|vector|vectors)(?:\.|$)/i.test(key) && !/(?:count|dimension|text)/i.test(key)) return REDACTED_VALUE;
      if (typeof item === "number" || typeof item === "boolean") {
        // Numeric registered secrets outrank even media ancestry and metric roles.
        if (secrets.includes(String(item))) return "<redacted>";
        const media = mediaValue(item); if (media !== undefined) return media;
        const raw = String(item), clean = string(raw);
        if (clean !== raw) return clean;
        return typeof item === "number" && !Number.isFinite(item) ? raw : item;
      }
      if (typeof item === "string") {
        if ((budget.remaining -= item.length) < 0) throw new Error("Privacy text budget");
        const redactedItem = redactValue(tc, item) as string;
        if (secrets.includes(redactedItem)) return "<redacted>";
        // Scrub the complete value once, with its original sibling/loss context.
        // Running scrubAttribute over the result used to repeat every JSON, URL,
        // assignment and media pass for large exception messages and stacks.
        if (lostContext && redactedItem.length >= valueLimit && /^\s*[\[{"]/.test(redactedItem)) {
          try { JSON.parse(redactedItem); } catch { return "<omitted: media>"; }
        }
        const skipName = sessionIdentity || admission && isCredentialName(key);
        // Name-only exceptions must not discard the original media path/hints.
        let clean = redactedItem === REDACTED_VALUE ? redactedItem : (skipName ? mediaValue(redactedItem) : undefined)
          ?? s1MediaPrefixReplacement(redactedItem)
          ?? scrubValue(redactedItem, secrets, skipName ? "" : key, 0, false, undefined, hints, true, false, lostContext, ambiguousBase64);
        // A shortened mapped URL still needs the conservative query policy.
        if (unattestedCopy && clean !== REDACTED_VALUE) clean = scrubAttribute(clean, secrets, true);
        return vectorPolicy(clean);
      }
      if (Array.isArray(item) && !types.isProxy(item)) {
        const length = data(item, "length");
        if (length > budget.nodes) throw new Error("Privacy array budget");
        const items = Array.from({ length }, (_, i) => data(item, String(i)));
        const media = mediaValue(items); if (media !== undefined) return media;
        return items.map((item, i) => value(item, `${key}.${i}`, depth + 1, admitted));
      }
      return string(item === null ? "null" : typeof item);
    };
    const clean: Attributes = Object.create(null);
    for (const [key, original] of entries(attrs)) {
      // Preserve optional usage/detail absence before value() can stringify it.
      if (original === undefined) continue;
      // Do this before parsing capsules, parser-bound responses, PII, hide
      // transforms or MIME inference: a fragment can hide the rest of a secret.
      if (!admission && (unknownHints || !NON_CONTENT_KEYS.has(key) && (withholdContent || scanBudget.exhausted || !exportedMarker(attrs, key, original) && truncatedValue(original, valueLimit, scanBudget, 0, true)))) {
        // A withheld session identity keeps its established H1 redaction marker;
        // both markers are value-free and the span still records unknown data.
        clean[string(key)] = withheldMarker(key);
        if (spanBudget) spanBudget.unknown = true;
        continue;
      }
      let item = original;
      if (key === PROJECTION_METADATA) {
        if (typeof item !== "string" || (budget.remaining -= item.length) < 0) throw new Error("Privacy capsule budget");
        const packed: unknown = JSON.parse(item);
        if (!packed || typeof packed !== "object" || Array.isArray(packed)) throw new Error("Privacy capsule shape");
        // A capsule is an attribute map, not an unrestricted JSON blob. Apply
        // every original key's hide family, including error/prompt/image rules.
        clean[key] = JSON.stringify(attributes(packed, secrets, operation, capsuleDepth + 1, admission, incomplete, valueLimit, unattestedCopy, spanType, responseContext, outputResponseContext, withholdContent, authoredHints, genkit));
        continue;
      }
      const mappedAlias = genkit && !!attrs && typeof attrs === "object" && isGenkitMappedAlias(attrs, key, original, tc);
      if (genkit && !mappedAlias && key !== "gen_ai.input.messages" && key !== "gen_ai.output.messages" &&
          hideSemanticContent(key.startsWith("genkit.usage.") ? key.slice("genkit.usage.".length) : key, { ...tc, hideLLMInvocationParameters: invocation })) {
        if (!key.endsWith("mime_type")) clean[key] = REDACTED_VALUE;
        continue;
      }
      // Genkit's shared side policy replaces the older broad alias rules only;
      // credentials, media, PII, inspection budgets and loss handling still run.
      if (!genkit) {
      // Native aliases can duplicate FI I/O under a different spelling. Treat
      // each opaque alias as a whole for granular flags, including image hiding.
      const inputAlias = /^(?:input\.|raw\.input$|gen_ai\.input\.|gen_ai\.prompt$|gen_ai\.system_instructions(?:\.|$)|gen_ai\.tool\.(?:call\.arguments|definitions|description)$|gen_ai\.request\.stop_sequences$)/.test(key)
        || /^mastra\..*\.(?:input|instructions)$/.test(key);
      const outputAlias = /^(?:output\.|raw\.output$|gen_ai\.output\.|gen_ai\.completion$|gen_ai\.tool\.call\.result$)/.test(key)
        || /^mastra\..*\.output$/.test(key)
        || (spanType === "model_step" && key === "mastra.metadata.body");
      const messages = key === "gen_ai.input.messages" || key === "gen_ai.output.messages";
      const requestMetadata = key === "gen_ai.request.parameters" || key === "gen_ai.tool.definitions";
      if (requestMetadata && !invocation && (hideInput || hideOutput || tc.hideInputImages)) {
        item = redactRequestMetadata(item, key);
      }
      if (!messages && !requestMetadata && ((inputAlias && (hideInput || tc.hideInputImages)) || (outputAlias && hideOutput)
        || (outputAlias && spanType === "rag_embedding" && tc.hideEmbeddingVectors))) {
        if (!key.endsWith("mime_type")) clean[key] = REDACTED_VALUE;
        continue;
      }
      if (tc.hideErrorDetails && spanType === "processor_run" && inputAlias && typeof item === "string") {
        if (item.length > budget.remaining) throw new Error("Privacy text budget");
        let parsed: unknown;
        try { parsed = JSON.parse(item); } catch { /* Non-JSON inputs are ordinary content. */ }
        // Only the native processor's top-level error member is error detail;
        // nested user messages and unrelated span types retain their input data.
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && typeof data(parsed, "error") === "string") {
          item = JSON.stringify({ ...parsed, error: REDACTED_VALUE });
        }
      }
      // Apply hiding by semantic family, not a list of particular leaf spellings.
      if (!messages && ((hideInput && /^(?:input\.|raw\.input$|gen_ai\.input\.|gen_ai\.system_instructions(?:\.|$))/.test(key)) ||
          (hideOutput && /^(?:output\.|raw\.output$|gen_ai\.output\.)/.test(key)))) {
        if (["input.value", "output.value", "raw.input", "raw.output"].includes(key)) clean[key] = "[REDACTED]";
        continue;
      }
      // Tool identity is not a tool payload. Value-level credential/media rules
      // still apply; this is not a token/id credential-name exemption.
      if ((hideInput || hideOutput || invocation) && key.startsWith("gen_ai.tool.") &&
          !["gen_ai.tool.call.id", "gen_ai.tool.name", "gen_ai.tool.type"].includes(key) &&
          !(requestMetadata && !invocation)) continue;
      if (invocation && key === "gen_ai.request.parameters") continue;
      if (invocation && /^gen_ai\.request\.(?:temperature|top_p|max_tokens|seed|frequency_penalty|presence_penalty)$/.test(key)) continue;
      if (hideInput && /embedding.*text/.test(key)) continue;
      if (!messages && tc.hideInputImages && (/^(?:input\.|raw\.input$|gen_ai\.input\.)/.test(key) || /(?:image|file).*(?:url|uri)|(?:url|uri).*(?:image|file)/i.test(key))) { clean[key] = "[REDACTED]"; continue; }
      if (key === "gen_ai.audio.transcript" && (operation === "text_to_speech" ? hideInput : hideOutput)) { clean[key] = "[REDACTED]"; continue; }
      }
      const name = string(key);
      requireInspection(scanBudget);
      const binding = providerResponseBinding(responseContext, key, item);
      if (binding && typeof item === "string") {
        if ((budget.remaining -= item.length) < 0) throw new Error("Privacy text budget");
        // Preserve the original text for the provider text-policy comparison;
        // pre-redacting PII would lose the signal that masks token bytes/logprobs.
        const capped = capBase64Images(item, tc.base64ImageMaxLength) as string;
        let payload: unknown;
        try { payload = JSON.parse(capped); } catch (error) {
          // The fail-closed cap can replace a whole JSON body with a bare marker
          // (for example above its scan limit). Withhold only this attribute, with a
          // fixed value-free marker, never partial content. Any other parse failure,
          // and any body that was not JSON, keeps the whole-span refusal.
          if ((capped !== IMAGE_CAP_WHOLE_MARKER && capped !== REDACTED_VALUE) || !isJsonText(item)) throw error;
          clean[name] = REDACTED_VALUE;
          continue;
        }
        const textPolicy = (text: string) => maskAttributes({ config: tc, attributes: { "output.value": text } })["output.value"];
        let sanitized = JSON.stringify(scrubProviderResponse(payload, secrets, binding.format, textPolicy, incomplete));
        // Preserve S1's opaque-JSON vector rule without re-entering generic token-name masking.
        if (tc.hideEmbeddingVectors && /\\*"(?:embedding|embeddings|vector|vectors)\\*"\s*:/i.test(sanitized)) sanitized = REDACTED_VALUE;
        const masked = maskAttributes({ config: tc, attributes: { [name]: sanitized } })[name];
        clean[name] = masked;
        // Rebind after optional PII/hide policy, never after an arbitrary host rewrite.
        // The cap's bare whole-value marker also starts with "[" but is no provider
        // body: like REDACTED_VALUE, it is never rebound or remembered.
        if (admission && typeof masked === "string" && /^[\[{]/.test(masked) && masked !== IMAGE_CAP_WHOLE_MARKER) binding.value = masked;
        // A second FI export boundary must re-check values without destroying safe
        // tokens. Carry origin only from this matched binding to its detached copy.
        if (outputResponseContext && typeof masked === "string" && /^[\[{]/.test(masked) && masked !== IMAGE_CAP_WHOLE_MARKER) {
          rememberProviderResponse(outputResponseContext, { [name]: masked }, binding.format);
        }
      } else {
        clean[name] = value(item, key);
        if (key === "session.id" && outputResponseContext && isSessionIdentity(responseContext, item) && typeof clean[name] === "string") {
          rememberSessionIdentity(outputResponseContext, clean[name] as string);
        }
      }
    }
    const masked = maskAttributes({ config: tc, attributes: clean });
    if (genkit && attrs && typeof attrs === "object") {
      for (const [key, original] of entries(attrs)) {
        if (isGenkitMappedAlias(attrs, key, original, tc) && clean[key] !== undefined) masked[key] = clean[key];
      }
      carryGenkitAliases(attrs, masked);
    }
    if (!admission) carryAttributeEvidence(attrs, masked);
    return masked;
  };
  // Two kinds of work. Marker probes and registered-secret matching run only on
  // content that may survive; a known-loss blanket replaces their output.
  // Credential DISCOVERY runs on every surface, withheld content included.
  // This callback charges real nested descriptor reads.
  const discoveryEntries = (value: unknown, budget: InspectionBudget): [string, any][] => {
    // A nested proxy VALUE keeps the non-executing type-token policy: it is
    // exported only as its type and never read. Attribute MAPS are enumerated
    // by entries() directly and still refuse proxies without running a trap.
    if (value && typeof value === "object" && types.isProxy(value)) { requireInspection(budget); return []; }
    const pairs = entries(value, budget);
    for (const [key, item] of pairs) requireInspection(budget, key.length + (typeof item === "string" ? item.length : 0));
    return pairs;
  };
  // Withheld content already debited ONCE when it was queued. Identity lookup
  // only: a queued host value may be a proxy, so no instanceof/prototype read.
  const reservedSources = new WeakMap<object, readonly [string | undefined, unknown]>();
  const paid = (key: string | undefined, value: unknown): object => {
    const token = {}; reservedSources.set(token, [key, value]); return token;
  };
  const reserve = (budget: InspectionBudget, key: string | undefined, value: unknown): object => {
    requireInspection(budget, (key?.length ?? 0) + (typeof value === "string" ? value.length : 0));
    return paid(key, value);
  };
  const discoverQueued = (sources: unknown[], budget: InspectionBudget, inspectText = false): void => {
    let incomplete = false;
    for (const value of sources) {
      if (budget.exhausted) { incomplete = true; break; }
      try {
        const reserved = value && typeof value === "object" ? reservedSources.get(value) : undefined;
        if (reserved) {
          // Same r9 JSON, URL-query and credential-name rules, but no inspect
          // callback: these characters are never debited a second time. Only
          // nested maps discovery materializes go through the budgeted adapter.
          requireInspection(budget);
          const [key, item] = reserved, root = {};
          const read = (node: object): [string, unknown][] => node === root && key !== undefined ? [[key, item]] : discoveryEntries(node, budget);
          for (const secret of discoverSecrets([key === undefined ? item : root], read)) registerSecret(secret);
          continue;
        }
        requireInspection(budget, typeof value === "string" ? value.length : 0);
        // Discovery's internal probes share the export ledger; admission keeps
        // its established once-per-source accounting for complete authored text.
        const inspect = inspectText ? (characters: number) => requireInspection(budget, characters, 0) : undefined;
        for (const secret of discoverSecrets([value], item => discoveryEntries(item, budget), inspect)) registerSecret(secret);
      } catch { incomplete = true; }
    }
    // One structurally refused source cannot skip later independent surfaces.
    // Incomplete accounting is never promoted to a successfully inspected view.
    if (incomplete) throw new Error("Privacy secret discovery incomplete");
  };
  const discoverAttributes = (attrs: unknown, responseContext?: object, admission = false, depth = 0, scanBudget = activeInspection ?? inspectionBudget(), _valueLimit = Infinity, withholdContent = false, queued?: unknown[]): void => {
    if (depth > 16) throw new Error("Privacy capsule budget");
    const sources = queued ?? [], ordinary: [string, unknown][] = [];
    for (const [key, item] of entries(attrs, scanBudget)) {
      // Queue preparation visits a node, not the ordinary text a second time.
      // The queue owns its scalar roots / map values; branches bypassing that
      // queue must debit their source bytes before parsing or provider traversal.
      requireInspection(scanBudget);
      if (item === undefined) continue;
      if (!admission && withholdContent && !nonContent(key, item)) {
        // Q8b replaces this value: no marker probe or registered-secret match
        // runs on it. Credential discovery still does, debited once here (key
        // and value) and never again by the queued scan. A bound provider
        // response keeps its format-aware discovery under that same debit.
        const binding = providerResponseBinding(responseContext, key, item);
        if (binding && typeof item === "string") {
          requireInspection(scanBudget, key.length + item.length);
          for (const secret of discoverProviderResponseSecrets(JSON.parse(item), binding.format)) registerSecret(secret);
        } else sources.push(reserve(scanBudget, key, item));
        continue;
      }
      if (key === PROJECTION_METADATA && typeof item === "string") {
        requireInspection(scanBudget, key.length + item.length);
        let packed: unknown;
        try { packed = JSON.parse(item); } catch { sources.push(item); continue; }
        discoverAttributes(packed, responseContext, admission, depth + 1, scanBudget, Infinity, false, sources);
        continue;
      }
      requireInspection(scanBudget);
      const binding = providerResponseBinding(responseContext, key, item);
      if (binding && typeof item === "string") {
        // Admission runs outside any export; nested admission inside an export is refused.
        if (admission && activeInspection) throw new Error("Privacy nested provider admission");
        // Provider parsing bypasses the ordinary queue: charge once here in
        // admission as well as export, before any traversal can start.
        requireInspection(scanBudget, key.length + item.length);
        for (const secret of discoverProviderResponseSecrets(JSON.parse(item), binding.format)) registerSecret(secret);
      } else {
        // A non-string bound value has no provider contract: treat it as uncertain.
        if (binding && spanBudget) spanBudget.unknown = true;
        // Conservative export fallback: discover ordinary credentials with the
        // budgeted adapter, then withhold, rather than invoke provider internals.
        if (key === "session.id" && isSessionIdentity(responseContext, item)) {
          requireInspection(scanBudget, key.length);
          sources.push(item);
        } else {
          // Admission deliberately does not discover original attribute names.
          // Its scalar queue still owns values; export's map callback owns both.
          if (admission) requireInspection(scanBudget, key.length);
          ordinary.push([key, item]);
        }
      }
    }
    sources.push(...(admission ? ordinary.map(([, item]) => item) : [Object.fromEntries(ordinary)]));
    if (!queued) discoverQueued(sources, scanBudget, !admission);
  };
  // Ordinary text policy is used only after complete discovery on a no-loss view.
  const text = (value: unknown, secrets: readonly string[]) => attributes({ value }, secrets, undefined, 0, true).value as string;
  const times = (value: unknown): [number, number] => [0, 1].map(i => hostCount(data(value, String(i)))) as [number, number];
  const identityOnly = (value: unknown): SpanContext => {
    const traceId = data(value, "traceId"), spanId = data(value, "spanId"), flags = data(value, "traceFlags");
    return { traceId: typeof traceId === "string" && /^[0-9a-f]{32}$/.test(traceId) ? traceId : INVALID_SPAN_CONTEXT.traceId, spanId: typeof spanId === "string" && /^[0-9a-f]{16}$/.test(spanId) ? spanId : INVALID_SPAN_CONTEXT.spanId, traceFlags: typeof flags === "number" && Number.isInteger(flags) && flags >= 0 && flags <= 255 ? flags : 0 };
  };
  const spanContext = (value: unknown, secrets: readonly string[]): SpanContext => {
    const raw = data(value, "traceState"), state: string[] = [];
    const add = (key: unknown, val: unknown) => {
      if (typeof key !== "string" || typeof val !== "string" || state.length >= 32) return;
      const cleanKey = text(key, secrets), clean = attributes({ [key]: val }, secrets)[cleanKey];
      if (typeof clean === "string") state.push(`${cleanKey}=${clean.replace(/[=,\r\n]/g, " ")}`);
    };
    const internal = data(raw, "_internalState"), serialized = data(raw, "_rawTraceState");
    if (internal && !types.isProxy(internal)) {
      try {
        if (activeInspection) requireInspection(activeInspection);
        const iterator = Map.prototype.entries.call(internal);
        for (let i = 0; i < 32; i++) { if (activeInspection) requireInspection(activeInspection); const item = iterator.next(); if (item.done) break; add(item.value[0], item.value[1]); }
      } catch { /* Unsupported tracestate representation: omit it. */ }
    }
    else if (typeof serialized === "string") for (const part of serialized.slice(0, 4096).split(",")) { const i = part.indexOf("="); if (i > 0) add(part.slice(0, i), part.slice(i + 1)); }
    return { ...identityOnly(value), ...(state.length ? { traceState: createTraceState(state.join(",")) } : {}) };
  };
  return {
    attributes,
    /** @internal Retain only tightening MIME/type/modality bits from FI-authored writes.
     * No values, native storage, completeness grants or safe-MIME evidence. */
    admissionHints: (attrs: unknown, previous: PrivacyHints = new Map()): PrivacyHints => {
      const budget = { remaining: 64 * 1024, nodes: 4096 };
      const charge = (value: unknown): unknown => {
        if (--budget.nodes < 0) throw new Error("Privacy authored hint budget");
        if (typeof value !== "string") return undefined;
        if ((budget.remaining -= value.length) < 0) throw new Error("Privacy authored hint budget");
        return value;
      };
      const items = entries(attrs).map(([key, value]): [string, unknown] => {
        charge(key);
        // Ask the catalog about MIME names without inspecting payload text.
        // Type and modality are the other discriminators understood by indexPrivacyHints.
        const leaf = key.split(/[.\[\]]+/).filter(part => part && !/^\d+$/.test(part)).at(-1);
        const discriminator = indexPrivacyHints([[key, undefined]]).size > 0 || ["type", "modality"].includes(leaf?.replace(/[-_]/g, "").toLowerCase() ?? "");
        if (!discriminator) return [key, undefined];
        if (Array.isArray(value) && !types.isProxy(value)) {
          const length = data(value, "length");
          if (length > budget.nodes) throw new Error("Privacy authored hint budget");
          return [key, Array.from({ length }, (_, i) => charge(data(value, String(i))))];
        }
        return [key, charge(value)];
      });
      const hints = new Map<string, readonly [boolean, boolean, boolean, boolean]>();
      for (const [path, bits] of indexPrivacyHints(items, "", previous)) {
        if (!bits[0] && !bits[1]) continue;
        charge(path);
        hints.set(path, [bits[0], bits[1], false, !!bits[3]]);
      }
      return hints;
    },
    admissionAttributes: (attrs: unknown, responseContext?: object, authoredHints?: PrivacyHints): Attributes => {
      // Harvest already-materialized JSON/URLs before scrubbing removes their
      // credential fields. Keep ordinary host metadata names out of admission
      // discovery; the final detached boundary recognizes those original keys.
      discoverAttributes(attrs, responseContext, true);
      return attributes(attrs, undefined, data(attrs, "gen_ai.operation.name"), 0, true, false, Infinity, false, undefined, responseContext, undefined, false, authoredHints);
    },
    span(span: ReadableSpan, requestSecrets: readonly string[] | null = [], extraResource: Attributes = {}): ReadableSpan {
      const previousBudget = spanBudget, previousScanBudget = spanScanBudget, previousInspection = activeInspection;
      const scanBudget = inspectionBudget();
      activeInspection = scanBudget; spanScanBudget = scanBudget;
      spanBudget = { remaining: 8 * 1024 * 1024, nodes: 20000, unknown: false };
      // Even failure to access the lineage registry must restore the active ledger.
      let cell: Lineage = { limit: Infinity, hostLoss: false, uncertain: false, authored: 0 };
      let rawContext: unknown, statusCode = 0;
      let identity: SpanContext = { ...INVALID_SPAN_CONTEXT };
      const base: any = { kind: 0, startTime: [0, 0], endTime: [0, 0], duration: [0, 0], ended: true, droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0 };
      try {
        rawContext = data(span, "_spanContext");
        if (!rawContext) {
          const method = data(span, "spanContext");
          if (typeof method === "function") { requireInspection(scanBudget); rawContext = Reflect.apply(method, span, []); }
        }
        identity = identityOnly(rawContext);
        const kind = data(span, "kind"); base.kind = [0, 1, 2, 3, 4].includes(kind) ? kind : 0;
        base.startTime = times(data(span, "startTime")); base.endTime = times(data(span, "endTime")); base.duration = times(data(span, "_duration") ?? data(span, "duration"));
        base.droppedAttributesCount = Math.max(hostCount(data(span, "_droppedAttributesCount")), hostCount(data(span, "droppedAttributesCount")));
        base.droppedEventsCount = Math.max(hostCount(data(span, "_droppedEventsCount")), hostCount(data(span, "droppedEventsCount")));
        base.droppedLinksCount = Math.max(hostCount(data(span, "_droppedLinksCount")), hostCount(data(span, "droppedLinksCount")));
        const rawParent = data(span, "parentSpanContext"), parentId = data(span, "parentSpanId");
        const parent = rawParent ?? (typeof parentId === "string" ? { ...identity, spanId: parentId } : undefined);
        if (parent) base.parentSpanContext = identityOnly(parent);
        const status = data(span, "status"), code = data(status, "code");
        statusCode = [0, 1, 2].includes(code) ? code : 0;
        const observedDrops = base.droppedAttributesCount > projectedAttributeDrops(span) || base.droppedEventsCount > 0 || base.droppedLinksCount > 0;
        cell.hostLoss = observedDrops;
        cell = bindLineage(lineage(span), rawContext);
        root(cell).hostLoss ||= observedDrops;
        let discoveryIncomplete = requestSecrets === null;
        const attemptDiscovery = (work: () => void): void => {
          if (scanBudget.exhausted) { discoveryIncomplete = true; return; }
          try { work(); } catch { discoveryIncomplete = true; }
        };
        const attrs = data(span, "attributes"), scope = data(span, "instrumentationScope") ?? data(span, "instrumentationLibrary"), resource = data(span, "resource");
        // Preparation refusals must not bypass later independent surfaces. Empty
        // local placeholders are never certified: the failure flag survives.
        let rawAttrs: unknown = {}, events: any[] = [], links: any[] = [];
        attemptDiscovery(() => { rawAttrs = readResource(resource, scanBudget); });
        attemptDiscovery(() => { events = surfaceArray(data(span, "events"), scanBudget); });
        attemptDiscovery(() => { links = surfaceArray(data(span, "links"), scanBudget); });
        const nativeLimit = data(span, "_attributeValueLengthLimit") ?? data(data(span, "_spanLimits"), "attributeValueLengthLimit");
        const priorLimit = root(cell).limit;
        const configuredLimit = nativeLimit ?? (Number.isFinite(priorLimit) ? priorLimit : undefined) ?? globalValueLimit() ?? Number(process.env.OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT ?? process.env.OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT);
        const valueLimit = Math.min(typeof configuredLimit === "number" && configuredLimit >= 0 ? configuredLimit : Infinity, priorLimit);
        const facts = inspectAttributeLoss(span, valueLimit, scanBudget, rawAttrs, extraResource, root(cell).uncertain, root(cell).hostLoss);
        retainLoss(cell, valueLimit, facts);
        discoveryIncomplete ||= facts.exhausted;
        for (const secret of requestSecrets ?? []) attemptDiscovery(() => { requireInspection(scanBudget, secret.length); registerSecret(secret); });
        const related: unknown[] = [];
        const sources: unknown[] = [];
        // Resolve tracestate loss before preparing the discovery queue, so a later
        // marker never sends already-withheld payload to marker/secret probes.
        // Tracestate is still discovered. Inspect data only, never a foreign serializer.
        const contexts: unknown[] = [rawContext, parent];
        for (const link of links) attemptDiscovery(() => { contexts.push(data(link, "context")); });
        // Each item is debited once here: by its loss probe, or, once loss is
        // already known (no marker probe can change that verdict), directly.
        const traceItems: [string | undefined, string][] = [];
        for (const context of contexts) attemptDiscovery(() => {
          const state = data(context, "traceState"), serialized = data(state, "_rawTraceState"), internal = data(state, "_internalState");
          if (typeof serialized === "string") {
            if (root(cell).hostLoss) requireInspection(scanBudget, serialized.length);
            else if (truncatedValue(serialized, Infinity, scanBudget)) root(cell).hostLoss = true;
            traceItems.push([undefined, serialized]);
          }
          if (internal && !types.isProxy(internal)) {
            requireInspection(scanBudget);
            try {
              const iterator = Map.prototype.entries.call(internal);
              for (let i = 0; i < 33; i++) {
                requireInspection(scanBudget);
                const next = iterator.next(); if (next.done) break;
                if (i === 32) throw new Error("Privacy tracestate count");
                const [key, value] = next.value;
                if (typeof key !== "string" || typeof value !== "string") throw new Error("Privacy tracestate shape");
                requireInspection(scanBudget, key.length + value.length);
                if (!root(cell).hostLoss && truncatedValue(value, Infinity, scanBudget)) root(cell).hostLoss = true;
                traceItems.push([key, value]);
              }
            } catch { throw new Error("Privacy tracestate discovery incomplete"); }
          }
        });
        const withholdContent = root(cell).hostLoss;
        // Withheld tracestate is discovered without a second debit.
        for (const [key, value] of traceItems) sources.push(withholdContent ? paid(key, value) : key === undefined ? value : { [key]: value });
        const discoverSurface = (surface: unknown, responseContext?: object): void => {
          related.push(surface);
          attemptDiscovery(() => discoverAttributes(surface, responseContext, false, 0, scanBudget, valueLimit, withholdContent, sources));
        };
        attemptDiscovery(() => discoverAttributes(attrs, lossContext(rawContext), false, 0, scanBudget, valueLimit, withholdContent, sources));
        discoverSurface(rawAttrs);
        attemptDiscovery(() => discoverSurface(data(scope, "attributes")));
        for (const event of events) attemptDiscovery(() => discoverSurface(data(event, "attributes")));
        for (const link of links) attemptDiscovery(() => discoverSurface(data(link, "attributes")));
        discoverSurface(extraResource);
        const discoverText = (value: unknown): void => {
          // Withheld names/messages are still discovered, debited once here.
          sources.push(withholdContent ? reserve(scanBudget, undefined, value) : value);
        };
        for (const [surface, key] of [[span, "name"], [status, "message"], [scope, "name"], [scope, "version"], [scope, "schemaUrl"]] as const) {
          attemptDiscovery(() => { discoverText(data(surface, key)); });
        }
        for (const event of events) attemptDiscovery(() => { discoverText(data(event, "name")); });
        attemptDiscovery(() => discoverQueued(sources, scanBudget, true));
        if (discoveryIncomplete || scanBudget.exhausted) throw new Error("Privacy loss or discovery incomplete");
        const imageReferences: string[] = [];
        if (tc.hideInputImages && !root(cell).hostLoss && !root(cell).uncertain) for (const surface of [attrs, ...related]) for (const [key, value] of entries(surface, scanBudget)) {
          requireInspection(scanBudget, key.length + (typeof value === "string" ? value.length : 0));
          if (typeof value === "string" && /(?:image|file).*(?:url|uri)|(?:url|uri).*(?:image|file)/i.test(key) && /^https?:\/\//i.test(value)) imageReferences.push(value);
        }
        // A provider fallback is privacy uncertainty, not an invented host count.
        if (spanBudget.unknown) root(cell).uncertain = true;
        if (root(cell).uncertain) throw new Error("Privacy lineage inspection incomplete");
        const unknownCopy = root(cell).hostLoss;
        const currentSecrets = snapshot(), secrets = imageReferences.length ? [...currentSecrets, ...imageReferences] : currentSecrets;
        const ctx = unknownCopy ? identity : spanContext(rawContext, secrets);
        bindLineage(cell, ctx);
        const unattestedCopy = nativeLimit === undefined && !Number.isFinite(priorLimit) && valueLimit < Infinity;
        const operation = data(attrs, "gen_ai.operation.name"), spanType = data(attrs, "mastra.span.type");
        // FI omissions may remove sibling context without being SDK loss. Apply
        // conservative media classification only to that projected attribute map;
        // Q8b's lineage-wide blanket remains gated solely by actual host loss.
        const projectedContext = projectedAttributeDrops(span) > 0;
        const genkit = data(scope, "name") === "genkit-tracer" || typeof data(attrs, "genkit:type") === "string";
        const cleanAttributes = (value: unknown, responseContext?: object) => attributes(value, secrets, operation, 0, false, unknownCopy || projectedContext && value === attrs, valueLimit, unattestedCopy, spanType, responseContext, responseContext ? ctx : undefined, unknownCopy, undefined, genkit);
        const boundaryText = (value: unknown, stripStack = false): string => unknownCopy ? HOST_TRUNCATED : text(stripStack && typeof value === "string" ? stripStackTrace(value) : value, secrets);
        const result = { ...base, spanContext: () => ctx, ...(parent ? { parentSpanContext: unknownCopy ? identityOnly(parent) : spanContext(parent, secrets) } : {}),
          name: boundaryText(data(span, "name") ?? ""), attributes: cleanAttributes(attrs, lossContext(rawContext)),
          resource: resourceFromAttributes({ ...cleanAttributes(rawAttrs), ...cleanAttributes(extraResource) }),
          instrumentationScope: { name: boundaryText(data(scope, "name") ?? ""), version: boundaryText(data(scope, "version") ?? ""), schemaUrl: boundaryText(data(scope, "schemaUrl") ?? ""), attributes: cleanAttributes(data(scope, "attributes")) },
          status: { code: statusCode, ...((unknownCopy || !tc.hideErrorDetails) && data(status, "message") !== undefined ? { message: boundaryText(data(status, "message"), true) } : {}) },
          events: events.map(event => ({ name: boundaryText(data(event, "name") ?? ""), time: times(data(event, "time")), attributes: cleanAttributes(data(event, "attributes")), droppedAttributesCount: Math.max(hostCount(data(event, "_droppedAttributesCount")), hostCount(data(event, "droppedAttributesCount"))) })),
          links: links.map(link => ({ context: unknownCopy ? identityOnly(data(link, "context")) : spanContext(data(link, "context"), secrets), attributes: cleanAttributes(data(link, "attributes")), droppedAttributesCount: Math.max(hostCount(data(link, "_droppedAttributesCount")), hostCount(data(link, "droppedAttributesCount"))) })),
        } as ReadableSpan;
        // Nothing partially built is returned after a late failure/observation.
        if (scanBudget.exhausted || spanBudget.unknown && !unknownCopy) throw new Error("Privacy incomplete output");
        const accounting = root(cell);
        if (accounting.autoAuthored || accounting.authored > 0) {
          // Incoming telemetry is not evidence of an FI-authored redaction. This
          // also rejects inflation on a mutated detached view and zero-count auto
          // spans, while leaving standalone projection's incoming count intact.
          if (accounting.authored > 0) result.attributes[DROPPED_CONTENT_COUNT] = accounting.authored;
          else delete result.attributes[DROPPED_CONTENT_COUNT];
        }
        if (unknownCopy) result.attributes[HOST_DATA_UNKNOWN] = true;
        rememberProjectedAttributeDrops(result, projectedAttributeDrops(span));
        bindLineage(cell, result); return result;
      } catch {
        root(cell).uncertain = true;
        // FI refusal is not host loss. Preserve validated structural fields and
        // discard all content without further host probes or stale exemptions.
        const result = { ...base, spanContext: () => identity, name: HOST_TRUNCATED, attributes: root(cell).hostLoss ? { [HOST_DATA_UNKNOWN]: true } : {}, status: { code: statusCode }, events: [], links: [], resource: resourceFromAttributes({}), instrumentationScope: { name: HOST_TRUNCATED } } as ReadableSpan;
        try {
          rememberProjectedAttributeDrops(result, projectedAttributeDrops(span));
          bindLineage(cell, identity); bindLineage(cell, result);
        } catch { /* Registry failure cannot expose a partial view. */ }
        return result;
      } finally { spanBudget = previousBudget; spanScanBudget = previousScanBudget; activeInspection = previousInspection; }
    },
  };
}
