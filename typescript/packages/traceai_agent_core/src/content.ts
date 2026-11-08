import { indexPrivacyHints, normalizePrivacyName } from "@traceai/fi-core/privacy";
import { generateTraceConfig, privacyFilter, seedSecrets, getRegisteredSecrets, type TraceConfigOptions } from "@traceai/fi-core";
import { redact } from "./privacy.js";
import { LIMITS } from "./schema.js";

const TRUNCATED = "[TRUNCATED]";
const bytes = (text: string): number => Buffer.byteLength(text);
// Keep fi-core recognition at the tracer boundary, not in lightweight hook code.
const contextKey = (key: string): boolean => ["type", "object"].includes(normalizePrivacyName(key)) ||
  indexPrivacyHints([[key, undefined]]).size > 0;

/** Resolve policy once, just like FITracer. Never shorten a value before the
 * complete credential, media, PII and embedding inspection has succeeded.
 * This is preparation, not an alternative to FITracer admission/export. */
export function createContentSanitizer(
  credentials: readonly string[] = [],
  options: TraceConfigOptions = {},
): (text: string, limit?: number) => string {
  const keys = [...credentials], policy = generateTraceConfig(options);
  const privacy = privacyFilter(policy, keys);
  return (text: string, limit = 20_000): string => {
    if (text.length > LIMITS.ingress || bytes(text) > LIMITS.ingress)
      return "[REDACTED_SIZE_LIMIT]";
    try {
      seedSecrets(process.env, keys);
      const key = "fi.agent.content";
      const secrets = [...new Set([...keys, ...getRegisteredSecrets()])].sort((a, b) => b.length - a.length);
      text = redact(text, secrets, contextKey);
      // Exact-value matching just consumed the complete registry snapshot above.
      // Apply the remaining FI policies without repeatedly charging every absent
      // credential against every quoted prose token. The actual FITracer still
      // performs its normal admission/discovery/export with the full registry.
      const clean = privacy.attributes({ [key]: text }, [])[key];
      if (typeof clean !== "string") return "[REDACTED]";
      return truncateContent(clean, limit);
    } catch {
      // Budget/discovery errors may never fall back to raw or partially read data.
      return "[REDACTED_PRIVACY_FAILURE]";
    }
  };
}

/** Internal: the input MUST already have passed complete-value privacy.
 * Retain valid JSON at every encoded layer for downstream FI privacy checks. */
export function truncateContent(text: string, limit = 20_000): string {
  if (!Number.isSafeInteger(limit) || limit < 64)
    throw new RangeError("Content limit must be at least 64 bytes");
  if (bytes(text) <= limit) return text;
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch {
    const buffer = Buffer.from(text);
    let end = limit;
    while ((buffer[end] & 0xc0) === 0x80) end--;
    return buffer.subarray(0, end).toString("utf8");
  }
  return shorten(parsed, limit) ?? JSON.stringify(TRUNCATED);
}

// Cost after escaping inside each enclosing JSON string, excluding its quotes.
function cost(text: string, layers: number): number {
  for (let i = 0; i < layers; i++) text = JSON.stringify(text).slice(1, -1);
  return bytes(text);
}
function shorten(value: unknown, budget: number, layers = 0, depth = 0): string | undefined {
  const full = JSON.stringify(value);
  if (cost(full, layers) <= budget) return full;
  const marker = JSON.stringify(TRUNCATED);
  if (depth > 16) return cost(marker, layers) <= budget ? marker : undefined;
  if (typeof value === "string") {
    let parsed: unknown;
    try { parsed = JSON.parse(value); } catch {
      let remaining = budget - cost(marker, layers);
      if (remaining < 0) return undefined;
      let prefix = "";
      for (const char of value) {
        const size = cost(JSON.stringify(char).slice(1, -1), layers);
        if (size > remaining) break;
        prefix += char;
        remaining -= size;
      }
      return JSON.stringify(prefix + TRUNCATED);
    }
    const inner = shorten(parsed, budget - cost('""', layers), layers + 1, depth + 1);
    return inner === undefined ? undefined : JSON.stringify(inner);
  }
  if (value === null || typeof value !== "object") return undefined;
  const array = Array.isArray(value);
  let markerKey = "_truncated";
  while (Object.hasOwn(value, markerKey)) markerKey += "_";
  const tail = array ? '{"_truncated":true}' : `${JSON.stringify(markerKey)}:true`;
  let remaining = budget - cost((array ? "[]" : "{}") + tail, layers);
  if (remaining < 0) return undefined;
  const parts: string[] = [];
  for (const [key, child] of Object.entries(value)) {
    const prefix = array ? "" : `${JSON.stringify(key)}:`;
    const overhead = cost(prefix + ",", layers);
    if (overhead > remaining) continue;
    const encoded = shorten(child, remaining - overhead, layers, depth + 1);
    if (encoded === undefined) {
      if (array) break; // Never shift array indices by skipping an item.
      continue;
    }
    parts.push(prefix + encoded);
    remaining -= overhead + cost(encoded, layers);
  }
  parts.push(tail);
  return (array ? "[" : "{") + parts.join(",") + (array ? "]" : "}");
}
