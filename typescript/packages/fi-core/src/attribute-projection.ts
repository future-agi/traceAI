import type { Attributes } from "@opentelemetry/api";

export const PROJECTION_METADATA = "traceai.attribute_projection.metadata";
const dropKey = "traceai.attribute_projection.dropped_count";
// Only bounded, canonical telemetry belongs in the overflow capsule. Unknown
// metadata can contain content (prompt variables, errors, image URLs, etc.).
const metadata = (key: string) => /^(?:gen_ai\.(?:span\.kind|operation\.name|provider\.name|request\.model|response\.(?:model|id|finish_reasons)|usage\.[a-z0-9_.]+)|fi\.span\.kind|error\.type|traceai\.(?:capture\.layer|wire\.(?:incomplete|truncated|body_unavailable|undecoded_encoding|timing_incomplete)|attribute_projection\.dropped_count))$/.test(key);
const priority = (key: string) => /^traceai\.(?:wire\.|attribute_projection)/.test(key) ? 0 :
  /^(?:gen_ai\.(?:span\.kind|operation\.name|provider\.name|request\.model|response\.(?:model|id|finish_reasons))|fi\.span\.kind|error\.type|traceai\.capture\.layer)$/.test(key) ? 1 : key.startsWith("gen_ai.usage.") ? 2 : 3;
const compare = (a: string, b: string) => priority(a) - priority(b) || (a < b ? -1 : a > b ? 1 : 0);
/** Deterministic pre-admission projection, shared by library and wire spans.
 * The capsule is re-sanitized by its original attribute names at export time.
 */
export function projectAttributes(attributes: Attributes, countLimit: number): { attributes: Attributes; dropped: number } {
  const limit = Number.isFinite(countLimit) ? Math.max(0, Math.floor(countLimit)) : 128;
  const keys = Object.keys(attributes);
  if (keys.length <= limit) return { attributes, dropped: 0 };
  if (!limit) return { attributes: {}, dropped: keys.length };
  const metas = keys.filter(metadata).sort(compare);
  const contents = keys.filter(key => !metadata(key)).sort((a, b) => {
    const rank = (key: string) => key === "output.value" ? 0 : key === "output.mime_type" ? 1 : key === "input.value" ? 2 : key.startsWith("gen_ai.output.") ? 3 : 4;
    return rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0);
  });
  const projected: Attributes = Object.create(null);
  let available = Math.max(0, limit - 1);
  const contentSlots = Math.min(contents.length, available > 1 ? 1 : 0);
  const metadataSlots = Math.max(0, available - contentSlots);
  const pack = metas.length > metadataSlots;
  const direct = Math.max(0, metadataSlots - (pack ? 1 : 0));
  for (const key of metas.slice(0, direct)) projected[key] = attributes[key];
  if (pack && metadataSlots) projected[PROJECTION_METADATA] = JSON.stringify(Object.fromEntries(metas.slice(direct).map(key => [key, attributes[key]])));
  available -= Object.keys(projected).length;
  for (const key of contents.slice(0, available)) projected[key] = attributes[key];
  const dropped = keys.filter(key => !Object.prototype.hasOwnProperty.call(projected, key)).length;
  // A one-slot host can retain only the degradation marker.
  if (Object.keys(projected).length === limit) delete projected[Object.keys(projected).at(-1)!];
  projected[dropKey] = dropped;
  return { attributes: projected, dropped };
}
/** SDK 1.x and 2.x expose this on their tracers; custom tracers use the SDK env fallback. */
export function tracerAttributeLimit(tracer: unknown): number {
  try {
    const value = (tracer as any).getSpanLimits?.().attributeCountLimit;
    if (typeof value === "number" && Number.isFinite(value)) return value;
  } catch { /* Non-SDK tracer. */ }
  const raw = process.env.OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT ?? process.env.OTEL_ATTRIBUTE_COUNT_LIMIT;
  const value = raw === undefined ? 128 : Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : 128;
}
