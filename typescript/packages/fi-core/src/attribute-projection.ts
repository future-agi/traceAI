import type { Attributes } from "@opentelemetry/api";
import { indexPrivacyHints, isZeroDetailDeferralKey, mediaReplacement } from "./privacy-runtime";
import { REDACTED_VALUE } from "./trace/trace-config/constants";

/** @deprecated Retired overflow capsule. Never produced; still re-sanitized by original key at export for older/foreign spans. */
export const PROJECTION_METADATA = "traceai.attribute_projection.metadata";
/** @deprecated Retired Node loss counter, superseded by {@link DROPPED_CONTENT_COUNT}. */
export const LEGACY_DROPPED_COUNT = "traceai.attribute_projection.dropped_count";
/** Spec A7: number of candidate CONTENT attribute keys the projection omitted. */
export const DROPPED_CONTENT_COUNT = "traceai.capture.dropped_content_count";
// Bounded, canonical telemetry that is exempt from the arbitrary-base64 heuristic at export.
// Deliberately narrower than the A7 protected set: widening it would weaken S6.
export const isProjectionMetadata = (key: string) => /^(?:gen_ai\.(?:span\.kind|operation\.name|provider\.name|request\.model|response\.(?:model|id|finish_reasons)|usage\.[a-z0-9_.]+)|fi\.span\.kind|error\.type|traceai\.(?:capture\.(?:layer|dropped_content_count)|wire\.(?:incomplete|truncated|body_unavailable|undecoded_encoding|timing_incomplete)|attribute_projection\.dropped_count))$/.test(key);

/** A7 protected families, in the order retained when even they exceed a host limit (rule 4):
 * degradation markers, identity, outcome, provider/model/operation, usage, TIMING, provenance.
 * Lexical key order inside each family. Shared Python/Go semantics plus the
 * existing TypeScript embedding.model_name alias. */
const PROTECTED_FAMILIES: readonly (readonly string[])[] = [
  [DROPPED_CONTENT_COUNT, "traceai.wire.body_unavailable", "traceai.wire.incomplete", "traceai.wire.timing_incomplete", "traceai.wire.timing_truncated", "traceai.wire.truncated", "traceai.wire.undecoded_encoding"],
  ["fi.span.kind", "gen_ai.response.id", "gen_ai.span.kind", "project_name", "project_type", "project_version_id", "project_version_name"],
  ["error.type", "gen_ai.response.finish_reasons", "http.response.status_code", "traceai.moderation.flagged"],
  // Legacy embedding model identity has the same retention priority as request.model.
  // Retention is not a value-scrubbing or arbitrary-base64 exemption.
  ["embedding.model_name", "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.model", "gen_ai.system"],
  [], // usage: every known gen_ai.usage.* / llm.token_count.* counter
  // Spec R7: exact present TIMING keys only, still subject to S1 privacy.
  ["gen_ai.response.latency", "gen_ai.response.time_to_first_chunk", "gen_ai.response.time_to_first_text", "gen_ai.response.time_to_first_token", "gen_ai.server.time_to_first_token", "gen_ai.server.time_per_output_token", "gen_ai.server.request.duration"],
  ["http.request.method", "server.address", "server.port", "traceai.capture.layer", "traceai.wire.endpoint"],
];
const FAMILY = new Map(PROTECTED_FAMILIES.flatMap((family, index) => family.map(key => [key, index] as const)));
const USAGE_FAMILY = 4;
// Zero-detail deferral is versioned policy data loaded once by privacy-runtime (the only module that may
// load privacy.json: its ESM build re-exports the CommonJS build), not a pattern over arbitrary usage keys.
// String zero and unknown/nonzero details retain their ordinary protected-family treatment.
export function protectedFamily(key: string): number | undefined {
  return FAMILY.get(key) ?? (key.startsWith("gen_ai.usage.") || key.startsWith("llm.token_count.") ? USAGE_FAMILY : undefined);
}

const MESSAGE = /^(gen_ai\.(input|output)\.messages|llm\.(input|output)_messages)\.(\d+)\./;
const INDEXED = /^(gen_ai\.tool\.definitions|gen_ai\.embeddings|embedding\.embeddings|retrieval\.documents|gen_ai\.retrieval\.documents)\.(\d+)\./;
const INPUT_CONTENT = ["input.", "gen_ai.input.", "gen_ai.tool.", "gen_ai.system_instructions", "gen_ai.prompt", "llm.prompt", "embedding.", "gen_ai.embeddings.", "llm.input_messages"];
const OUTPUT_CONTENT = ["output.", "gen_ai.output.", "llm.output_messages", "retrieval.", "gen_ai.retrieval."];
const GENERIC = 2 ** 30;
/** [tier, order, group id]: 0 output messages (ascending), 1 output value/transcript
 * representations, 2 input messages (newest first), 3 other input content, 4 aggregate input.
 * Undefined for noncontent metadata. */
type Group = readonly [number, number, string];
function contentGroup(key: string, operation: string): Group | undefined {
  const message = MESSAGE.exec(key);
  if (message) {
    const index = Number(message[4]);
    return (message[2] ?? message[3]) === "output" ? [0, index, message[0]] : [2, -index, message[0]];
  }
  if (key === "output.value" || key === "output.mime_type") return [1, 0, "output.value"];
  if (key === "input.value" || key === "input.mime_type") return [4, 0, "input.value"];
  if (key === "gen_ai.audio.transcript") return operation === "text_to_speech" ? [3, 0, key] : [1, 2, key];
  if (key === "raw.output") return [1, 1, key];
  const indexed = INDEXED.exec(key);
  if (indexed) return [indexed[1].endsWith("retrieval.documents") ? 1 : 3, Number(indexed[2]) + 10, indexed[0]];
  if (key === "raw.input" || key === "gen_ai.request.parameters" || key === "llm.invocation_parameters" || INPUT_CONTENT.some(prefix => key.startsWith(prefix))) return [3, GENERIC, key];
  if (OUTPUT_CONTENT.some(prefix => key.startsWith(prefix))) return [1, GENERIC, key];
  return undefined;
}
const lexical = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;
const compareGroups = (a: Group, b: Group) => a[0] - b[0] || a[1] - b[1] || lexical(a[2], b[2]);

/** A generic content key and its media MIME/type companion form one atomic group:
 * the companion anchors its parent path; every generic content key below the
 * outermost anchor joins it with the group's highest-priority tier. */
function companionGroups(groups: Map<string, { group: Group; keys: string[] }>, values: Attributes): { group: Group; keys: string[] }[] {
  const generic = [...groups.values()].filter(item => item.group[1] === GENERIC && item.keys.length === 1 && item.group[2] === item.keys[0]);
  const anchors = [...indexPrivacyHints(generic.map(item => [item.keys[0], values[item.keys[0]]] as const))]
    .filter(([parent, hint]) => parent && (hint[0] || hint[1])).map(([parent]) => parent)
    .sort((a, b) => a.length - b.length || lexical(a, b));
  if (!anchors.length) return [...groups.values()];
  const merged: { group: Group; keys: string[] }[] = [], members = new Map<string, { group: Group; keys: string[] }[]>(), isGeneric = new Set(generic);
  for (const item of groups.values()) {
    const parent = isGeneric.has(item) ? anchors.find(anchor => item.keys[0].startsWith(anchor + ".")) : undefined;
    if (parent === undefined) { merged.push(item); continue; }
    const list = members.get(parent);
    if (list) list.push(item); else members.set(parent, [item]);
  }
  for (const [parent, items] of members) merged.push({ group: [Math.min(...items.map(item => item.group[0])), GENERIC, parent + "."], keys: items.flatMap(item => item.keys).sort(lexical) });
  return merged;
}

/** Deterministic pre-admission projection (spec A7), shared by library and wire spans.
 * Protected semantics are reserved before content, except that zero-valued usage
 * details may yield just enough capacity for canonical output and its earlier output
 * groups. Bounded content fits the remainder (output first, newest input suffix; atomic);
 * `traceai.capture.dropped_content_count` accumulates the omitted content keys.
 * `dropped` is every candidate key not emitted, for OTel's separate all-attribute counter.
 * The same candidates and limit select the same attributes whatever the insertion order.
 */
export function projectAttributes(attributes: Attributes, countLimit: number, operation?: string): { attributes: Attributes; dropped: number } {
  const limit = Number.isFinite(countLimit) ? Math.max(0, Math.floor(countLimit)) : 128;
  // An absent OTel attribute is not a protected counter or a dropped candidate.
  // Do not coerce it to a string or confuse actual absence with string zero,
  // literal "undefined", or an unknown but populated usage detail.
  const items = Object.entries(attributes).filter(([, value]) => value !== undefined);
  if (items.length > 4096) {
    // Sibling recognition would be cropped: fail closed for content, keep protected fields.
    const values: Attributes = Object.fromEntries(items.filter(([key]) => key !== DROPPED_CONTENT_COUNT && protectedFamily(key) !== undefined));
    const previous = attributes[DROPPED_CONTENT_COUNT];
    // Unclassified keys may carry content (prompt variables, errors): count them all.
    const content = items.filter(([key]) => key !== DROPPED_CONTENT_COUNT && protectedFamily(key) === undefined).length;
    return select(values, limit, typeof previous === "number" && Number.isInteger(previous) && previous > 0 ? previous : 0, [], [], content, items.filter(([key]) => key !== DROPPED_CONTENT_COUNT).length);
  }
  // Project only after consulting the COMPLETE sibling set: projection must never
  // turn a recognized payload into an unmarked scalar by evicting its MIME/type companion.
  const hints = indexPrivacyHints(items);
  const protect = (value: any, key: string): any => {
    if (value === REDACTED_VALUE) return value;
    const omitted = mediaReplacement(value, key, hints);
    if (omitted !== undefined) return omitted;
    if (Array.isArray(value)) {
      if (value.length > 16384) return "<omitted: media>";
      return value.map((item, i) => item === REDACTED_VALUE ? item : mediaReplacement(item, `${key}.${i}`, hints) ?? item);
    }
    return value;
  };
  const values: Attributes = Object.fromEntries(items.map(([key, value]) => [key, protect(value, key)]));
  if (items.length <= limit) return { attributes: values, dropped: 0 };
  const previousRaw = values[DROPPED_CONTENT_COUNT];
  delete values[DROPPED_CONTENT_COUNT];
  const previous = typeof previousRaw === "number" && Number.isInteger(previousRaw) && previousRaw > 0 ? previousRaw : 0;
  const op = operation ?? (typeof values["gen_ai.operation.name"] === "string" ? values["gen_ai.operation.name"] : "");
  const protectedKeys: Attributes = {}, metadata: string[] = [], content = new Map<string, { group: Group; keys: string[] }>();
  for (const key of Object.keys(values)) {
    if (protectedFamily(key) !== undefined) { protectedKeys[key] = values[key]; continue; }
    const group = contentGroup(key, op);
    if (!group) { metadata.push(key); continue; }
    const id = `${group[0]}\u0000${group[1]}\u0000${group[2]}`;
    const entry = content.get(id) ?? { group, keys: [] };
    entry.keys.push(key); content.set(id, entry);
  }
  const groups = companionGroups(content, values).sort((a, b) => compareGroups(a.group, b.group));
  return select(values, limit, previous, groups, metadata.sort(lexical), groups.reduce((sum, item) => sum + item.keys.length, 0), Object.keys(values).length, Object.keys(protectedKeys));
}

/** `candidates` counts candidate keys other than a pre-existing content-count marker. */
function select(values: Attributes, capacity: number, previous: number, groups: { group: Group; keys: string[] }[], metadata: string[], contentKeys: number, candidates: number,
  protectedNames: string[] = Object.keys(values)): { attributes: Attributes; dropped: number } {
  let protectedOrder = protectedNames.map(key => [protectedFamily(key)!, key] as const).sort((a, b) => a[0] - b[0] || lexical(a[1], b[1])).map(([, key]) => key);
  const canonical = groups.find(item => item.group[2] === "output.value" && item.keys.includes("output.value"));
  const choose = (room: number): [string[], number] => {
    const chosen: string[] = [];
    let outputOmitted = false, inputOmitted = false;
    for (const { group: [tier], keys } of groups) {
      if (tier >= 2 && outputOmitted) break; // never admit input while higher-priority output is omitted
      if ((tier === 2 || tier === 4) && inputOmitted) continue; // contiguous newest suffix; aggregate cannot restore it
      if (keys.length <= room) { chosen.push(...[...keys].sort(lexical)); room -= keys.length; }
      else if (tier <= 1) outputOmitted = true;
      else if (tier === 2) inputOmitted = true;
    }
    return [chosen, contentKeys - chosen.length];
  };
  const selectContent = (protectedCount: number) => {
    let room = capacity - protectedCount;
    let [chosen, omitted] = choose(Math.max(0, room));
    const marker = previous > 0 || omitted > 0;
    if (marker) { room -= 1; [chosen, omitted] = choose(Math.max(0, room)); } // one slot, reserved before writing
    return { room, chosen, omitted, marker };
  };
  let selection = selectContent(protectedOrder.length);
  if (canonical && !selection.chosen.includes("output.value")) {
    // An atomic output/MIME pair must not lose to raw.output just because zero
    // detail counters left one slot. Only defer the smallest zero-detail suffix
    // that admits canonical output with the SAME group/marker selection rules.
    // Canonical totals, nonzero details, TIMING and unknown usage never yield.
    const zeroDetails = protectedOrder.filter(key => isZeroDetailDeferralKey(key) && values[key] === 0);
    for (let count = 1; count <= zeroDetails.length; count++) {
      const attempt = selectContent(protectedOrder.length - count);
      if (!attempt.chosen.includes("output.value")) continue;
      const deferred = new Set(zeroDetails.slice(-count));
      protectedOrder = protectedOrder.filter(key => !deferred.has(key));
      // Still metadata: dropping these affects the all-attribute counter, not
      // the content marker. Retain them if atomic grouping leaves spare space.
      metadata = [...deferred, ...metadata];
      selection = attempt;
      break;
    }
  }
  let { room } = selection;
  const { chosen, omitted, marker } = selection;
  const selected: Attributes = {};
  if (room <= 0) {
    // Protected semantics plus the reserved marker fill (or exceed) capacity:
    // no content, family-ordered protected prefix including the leading marker.
    const kept = [...(marker ? [DROPPED_CONTENT_COUNT] : []), ...protectedOrder].slice(0, capacity);
    for (const key of kept) selected[key] = key === DROPPED_CONTENT_COUNT ? previous + contentKeys : values[key];
    return { attributes: selected, dropped: candidates - kept.filter(key => key !== DROPPED_CONTENT_COUNT).length };
  }
  for (const key of protectedOrder) selected[key] = values[key];
  for (const key of chosen) selected[key] = values[key];
  room -= chosen.length;
  // Optional noncontent metadata only after protected fields and bounded content.
  for (const key of metadata) { if (room <= 0) break; selected[key] = values[key]; room--; }
  if (marker) selected[DROPPED_CONTENT_COUNT] = previous + omitted;
  return { attributes: selected, dropped: candidates - (Object.keys(selected).length - (marker ? 1 : 0)) };
}
/** SDK 1.x and 2.x expose this on their tracers; custom tracers use the SDK env fallback. */
export function tracerAttributeLimit(tracer: unknown): number {
  try {
    // SDK releases may expose only the native data slot. A provider configured
    // in code must not silently fall back to the environment default.
    const limits = (tracer as any).getSpanLimits?.() ?? (tracer as any)._spanLimits;
    const value = limits?.attributeCountLimit;
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) return value;
  } catch { /* Non-SDK tracer. */ }
  const raw = process.env.OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT ?? process.env.OTEL_ATTRIBUTE_COUNT_LIMIT;
  const value = raw === undefined ? 128 : Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : 128;
}
