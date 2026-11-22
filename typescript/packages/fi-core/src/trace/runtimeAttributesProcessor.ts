import type { AttributeValue, Attributes, Context } from "@opentelemetry/api";
import type { Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { ADOPT_PROVIDER_POLICY, runtimeProcessorInstances, runtimeSpans } from "../runtime-metadata";
import { runtimeAttributesEnabled } from "./runtimeAttributesEnabled";
import { rememberAutoSpanSink } from "../auto-span-delivery";
import { maskAttributes } from "./trace-config/maskingRules";
import { generateTraceConfig } from "./trace-config/traceConfig";
import type { TraceConfig, TraceConfigOptions } from "./trace-config/types";

const CARRIER_KEY = Symbol.for("futureagi.runtime.span_attributes.v1");
const REGISTRY_KEY = Symbol.for("futureagi.runtime.skills.v1");
const AGENT_SOURCES = Symbol.for("futureagi.runtime.context-agent.sources.v1");
const PROCESSOR_KEY = Symbol.for("futureagi.runtime.skills.processor.v1");
interface RegistryReader {
  v: number;
  get(traceId: string): unknown;
}

// The same limits as Python's using_runtime_attributes (runtime_attributes.py),
// pinned by the shared processor_v1.json normalization vectors.
const PREFIX = /^(fi\.skills\.|fi\.context\.|gen_ai\.agent\.)/;
const SKILL_LISTS: ReadonlySet<string> = new Set(["fi.skills.version_ids", "fi.skills.ids", "fi.skills.refs"]);
const MAX_ATTRIBUTES = 64;
const MAX_SKILLS = 32;
const MAX_STRING_BYTES = 256;
// Keys examined per snapshot, not output: a large foreign snapshot cannot add
// input-sized work to every span start.
const MAX_SCAN = 256;
const short = (value: unknown): value is string => typeof value === "string" && Buffer.byteLength(value, "utf8") <= MAX_STRING_BYTES;

/** Bounded normalization of a carrier or registry snapshot; never propagates fi.skill.*.
 * `cacheable` only when the snapshot and every array read from it are frozen. */
function normalizeRuntimeAttributes(input: unknown): { attributes: Attributes; cacheable: boolean } {
  const result: Record<string, AttributeValue> = {};
  if (!input || typeof input !== "object" || Array.isArray(input)) return { attributes: result, cacheable: false };
  const source = input as Record<string, unknown>;
  let cacheable = Object.isFrozen(source), truncated = false, scanned = 0, kept = 0;
  for (const key in source) {
    if (++scanned > MAX_SCAN) break;
    if (!Object.prototype.hasOwnProperty.call(source, key)) continue;
    if (!short(key) || !PREFIX.test(key)) continue;
    if (key === "fi.context.linked_skill_version_ids") continue; // context.load only, not loaded skills
    const value = source[key];
    let retained: AttributeValue;
    if (typeof value === "string") {
      if (!short(value)) continue;
      retained = value;
    } else if (typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))) {
      retained = value;
    } else if (Array.isArray(value)) {
      if (!Object.isFrozen(value)) cacheable = false;
      const skillList = SKILL_LISTS.has(key), limit = skillList ? MAX_SKILLS : MAX_ATTRIBUTES;
      // Inspect at most `limit` items; holes and invalid items reject the list
      // rather than misalign parallel identity lists.
      const head: string[] = [];
      let valid = true;
      for (let i = 0; i < Math.min(value.length, limit); i++) {
        const item: unknown = value[i];
        if (!short(item)) { valid = false; break; }
        head.push(item);
      }
      if (!valid) continue;
      if (skillList) truncated ||= value.length > MAX_SKILLS;
      else if (value.length > limit) continue;
      retained = head;
    } else continue;
    if (!Object.prototype.hasOwnProperty.call(result, key)) kept++;
    result[key] = retained;
    // Leave room for the derived skill count and truncation flag below.
    if (kept === MAX_ATTRIBUTES - 2) break;
  }
  const versions = result["fi.skills.version_ids"];
  if (Array.isArray(versions)) result["fi.skills.count"] = versions.length;
  if (truncated) result["fi.skills.truncated"] = true;
  else if (result["fi.skills.truncated"] !== true) delete result["fi.skills.truncated"];
  return { attributes: result, cacheable };
}

/** Per-setting strictest of two policies: any hide/redaction switch on, the smaller image cap. */
function strictest(a: TraceConfig, b: TraceConfig): TraceConfig {
  const merged: Record<string, boolean | number> = {};
  for (const key of Object.keys(a) as (keyof TraceConfig)[]) {
    const x = a[key], y = b[key];
    merged[key] = typeof x === "number" && typeof y === "number" ? Math.min(x, y) : Boolean(x) || Boolean(y);
  }
  return merged as TraceConfig;
}

export interface FutureAGIRuntimeAttributesSpanProcessorOptions {
  /** The provider's export privacy policy. Defaults to the FI_* environment, as FITracer does. */
  traceConfig?: TraceConfigOptions;
}

/**
 * Stamps later spans from the versioned carrier or the optional SDK registry.
 * Opt-in only: install with `runtimeAttributes: true` or pass it explicitly in
 * `spanProcessors`. It runs ahead of exporters and application processors, so
 * values are masked with the provider's policy before they reach the span.
 * No SDK dependency, I/O, resource mutation, span retention or end-of-span work.
 */
export class FutureAGIRuntimeAttributesSpanProcessor implements SpanProcessor {
  private config: TraceConfig;
  // Masked results per frozen snapshot; never holds a span or a mutable source.
  private cache = new WeakMap<object, { raw: Attributes; masked: Attributes }>();

  constructor(options: FutureAGIRuntimeAttributesSpanProcessorOptions = {}) {
    this.config = generateTraceConfig(options.traceConfig);
    // An onStart observer is not an export destination for auto instrumentation.
    rememberAutoSpanSink(this);
    runtimeProcessorInstances.add(this);
  }

  /** @internal Called by a provider that receives this processor in `spanProcessors`. */
  [ADOPT_PROVIDER_POLICY](options?: TraceConfigOptions): void {
    this.config = strictest(this.config, generateTraceConfig(options));
    this.cache = new WeakMap();
  }

  private stamped(source: unknown): { raw: Attributes; masked: Attributes } {
    if (source && typeof source === "object") {
      const hit = this.cache.get(source);
      if (hit) return hit;
    }
    const { attributes: raw, cacheable } = normalizeRuntimeAttributes(source);
    const result = { raw, masked: maskAttributes({ config: this.config, attributes: raw }) };
    if (cacheable) this.cache.set(source as object, result);
    return result;
  }

  onStart(span: Span, parentContext: Context): void {
    try {
      const globals = globalThis as unknown as Record<symbol, unknown>;
      if (!runtimeAttributesEnabled()) return;
      // Per-span opt-in evidence for this provider's FI wrappers and registry.
      runtimeSpans.add(span);
      const traceId = span.spanContext().traceId;
      // The SDK status helper uses only this last-seen trace id, never a span.
      globals[PROCESSOR_KEY] = traceId;
      const carrier = parentContext.getValue(CARRIER_KEY);
      let source: unknown;
      if (carrier !== undefined && carrier !== null) {
        // Even an empty/unknown carrier is authoritative: no stale registry mix.
        if (typeof carrier === "object" && "v" in carrier && carrier.v === 1 && "attrs" in carrier) source = carrier.attrs;
      } else {
        const registry = globals[REGISTRY_KEY] as RegistryReader | undefined;
        if (registry?.v === 1 && typeof registry.get === "function") source = registry.get(traceId);
      }
      const { raw, masked } = this.stamped(source);
      for (const [key, value] of Object.entries(masked)) {
        if (value === undefined || Object.prototype.hasOwnProperty.call(span.attributes, key)) continue;
        span.setAttribute(key, value);
        // Optional SDK writer observation for this exact published map. Never
        // infer a generated name from equal strings or arbitrary carrier keys,
        // and never report a name that masking changed.
        if (key === "gen_ai.agent.name" && value === raw[key] && source && typeof source === "object") {
          const observers = globals[AGENT_SOURCES] as WeakMap<object, (span: Span) => void> | undefined;
          observers?.get(source)?.(span);
        }
      }
    } catch {
      // Telemetry must not affect application execution, including foreign
      // carrier/registry implementations. Never log their possibly private data.
    }
  }

  onEnd(): void { /* No end work. */ }
  forceFlush(): Promise<void> { return Promise.resolve(); }
  shutdown(): Promise<void> { return Promise.resolve(); }
}
