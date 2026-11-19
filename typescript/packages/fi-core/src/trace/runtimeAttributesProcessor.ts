import type { Context } from "@opentelemetry/api";
import type { Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { rememberAutoSpanSink } from "../auto-span-delivery";

const CARRIER_KEY = Symbol.for("futureagi.runtime.span_attributes.v1");
const REGISTRY_KEY = Symbol.for("futureagi.runtime.skills.v1");
const PROCESSOR_KEY = Symbol.for("futureagi.runtime.skills.processor.v1");
// Optional SDK configuration bridge. Without the SDK, environment controls work.
// The SDK owns publishing this reader; fi-core never creates SDK state.
const CONFIG_KEY = Symbol.for("futureagi.runtime.skills.config.v1");
interface ConfigurationReader {
  v: 1;
  mode(): "on" | "off" | "load_only";
}

type RuntimeAttributes = Record<string, string | number | boolean | string[]>;
interface RegistryReader {
  v: number;
  get(traceId: string): unknown;
}

/** Same bounded prefix contract as the optional SDK; never propagate fi.skill.*. */
function allowlisted(input: unknown): RuntimeAttributes {
  const result: RuntimeAttributes = {};
  if (!input || typeof input !== "object" || Array.isArray(input)) return result;
  for (const [key, value] of Object.entries(input)) {
    if (Object.keys(result).length >= 64) break;
    if (key === "fi.context.linked_skill_version_ids") continue; // context.load only, not loaded skills
    if (Buffer.byteLength(key, "utf8") > 256 || !/^(fi\.skills\.|fi\.context\.|gen_ai\.agent\.)/.test(key)) continue;
    if (typeof value === "string" && Buffer.byteLength(value, "utf8") <= 256) result[key] = value;
    else if (typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))) result[key] = value;
    else if (Array.isArray(value) && value.every(item => typeof item === "string" && Buffer.byteLength(item, "utf8") <= 256)) {
      result[key] = value.slice(0, 32);
    }
  }
  return result;
}

/**
 * Stamps later spans from the versioned carrier or the optional SDK registry.
 * Install in the provider constructor, ahead of exporters. No SDK dependency,
 * I/O, resource mutation, span retention or end-of-span work.
 */
export class FutureAGIRuntimeAttributesSpanProcessor implements SpanProcessor {
  constructor() {
    // An onStart observer is not an export destination for auto instrumentation.
    rememberAutoSpanSink(this);
  }

  onStart(span: Span, parentContext: Context): void {
    try {
      const globals = globalThis as unknown as Record<symbol, unknown>;
      const configuration = globals[CONFIG_KEY] as ConfigurationReader | undefined;
      if (configuration?.v === 1 && typeof configuration.mode === "function") {
        // A live SDK reader is authoritative; an invalid answer cannot enable stamping.
        if (configuration.mode() !== "on") return;
      } else {
        const mode = process.env.FI_SKILLS_TRACING;
        if (mode === "off" || mode === "load_only") return;
      }
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
      for (const [key, value] of Object.entries(allowlisted(source))) {
        if (!Object.prototype.hasOwnProperty.call(span.attributes, key)) span.setAttribute(key, value);
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
