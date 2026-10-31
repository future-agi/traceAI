import { types } from "node:util";
import type { AnyExportedSpan } from "@mastra/core/observability";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

// Capture intrinsics once. Never read methods, constructor names, @@toStringTag,
// iterators or inspection hooks from an application value (including a Proxy).
const { isProxy, isDate } = types;
const getDescriptor = Object.getOwnPropertyDescriptor;
const getPrototype = Object.getPrototypeOf;
const keys = Object.keys;
const create = Object.create;
const setPrototype = Object.setPrototypeOf;
const apply = Reflect.apply;
const dateTime = Date.prototype.getTime;
const dateISO = Date.prototype.toISOString;
const NativeDate = Date;
const objectPrototype = Object.prototype;
const defineProperty = Object.defineProperty;

export function ownData(value: unknown, key: PropertyKey): unknown {
  if (!value || typeof value !== "object" || isProxy(value)) return undefined;
  const descriptor = getDescriptor(value, key);
  return descriptor && "value" in descriptor ? descriptor.value : undefined;
}

interface Budget { nodes: number; chars: number }
const budget = (): Budget => ({ nodes: 20000, chars: 8 * 1024 * 1024 });

/** Data-only, detached, JSON-safe snapshot. Unsupported values use typeof names. */
function copy(value: unknown, remaining: Budget, ancestors: Set<object>, depth: number): unknown {
  if (--remaining.nodes < 0 || depth > 32) return typeof value;
  if (typeof value === "string") {
    remaining.chars -= value.length;
    return remaining.chars >= 0 ? value : "string";
  }
  if (value === null || value === undefined || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (typeof value !== "object" || isProxy(value)) return typeof value;
  if (isDate(value)) {
    // Date#toJSON, overridden getTime/toISOString and subclass methods are never
    // consulted. Even invalid dates have a safe, non-coercing representation.
    return Number.isFinite(apply(dateTime, value, [])) ? apply(dateISO, value, []) : "Date";
  }
  if (ancestors.has(value)) return "object";
  const array = Array.isArray(value);
  if (!array) {
    const prototype = getPrototype(value);
    if (prototype !== null && prototype !== objectPrototype) return "object";
  }
  const names = array ? undefined : keys(value);
  const size = array ? ownData(value, "length") as number : names!.length;
  if (size > 4096 || size > remaining.nodes) return "object";
  // Objects have no inherited hooks. Arrays keep intrinsic array methods needed
  // by downstream OTel adapters, but never inherit an application toJSON hook.
  const result: any = array ? defineProperty([], "toJSON", { value: undefined }) : create(null);
  ancestors.add(value);
  try {
    for (let i = 0; i < size; i++) {
      const key = array ? String(i) : names![i];
      remaining.chars -= key.length;
      if (remaining.nodes <= 0 || remaining.chars < 0) break;
      const descriptor = getDescriptor(value, key);
      if (descriptor && "value" in descriptor) result[key] = copy(descriptor.value, remaining, ancestors, depth + 1);
      else if (array) result[key] = "undefined";
    }
  } finally { ancestors.delete(value); }
  return result;
}

export function snapshotData(value: unknown): unknown {
  return copy(value, budget(), new Set(), 0);
}

function detachedDate(value: unknown): Date | undefined {
  if (!value || typeof value !== "object" || isProxy(value) || !isDate(value)) return undefined;
  const time = apply(dateTime, value, []);
  if (!Number.isFinite(time)) return undefined;
  const detached = setPrototype(new NativeDate(time), null);
  // The converter invokes these methods only on documented timing fields.
  // JSON serialization, if needed, also goes straight to the native ISO method.
  defineProperty(detached, "getTime", { value: dateTime });
  defineProperty(detached, "toISOString", { value: dateISO });
  defineProperty(detached, "toJSON", { value: dateISO });
  return detached;
}

/** The upstream converter requires Dates for span/model timing, not application I/O. */
export function snapshotMastraSpan(value: unknown): AnyExportedSpan {
  const snapshot = snapshotData(value) as any;
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) throw new Error("Invalid Mastra snapshot");
  for (const key of ["startTime", "endTime"]) {
    const date = detachedDate(ownData(value, key));
    if (date) snapshot[key] = date;
  }
  const completion = detachedDate(ownData(ownData(value, "attributes"), "completionStartTime"));
  if (completion && snapshot.attributes && typeof snapshot.attributes === "object") snapshot.attributes.completionStartTime = completion;
  return snapshot;
}

/** Legacy SDK spans expose public getters; read only known own data backing slots. */
export function snapshotReadableSpan(value: unknown): ReadableSpan {
  const projected: Record<string, unknown> = create(null);
  for (const key of ["name", "kind", "startTime", "endTime", "status", "attributes", "links", "events", "ended", "parentSpanId", "parentSpanContext", "instrumentationScope", "instrumentationLibrary"]) {
    projected[key] = ownData(value, key);
  }
  for (const key of ["duration", "droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"]) {
    projected[key] = ownData(value, key) ?? ownData(value, `_${key}`);
  }
  projected._spanContext = ownData(value, "_spanContext");
  const resource = ownData(value, "resource");
  let attributes = ownData(resource, "attributes") ?? ownData(resource, "_attributes");
  if (attributes === undefined) {
    attributes = create(null);
    const pairs = ownData(resource, "_rawAttributes");
    const length = ownData(pairs, "length");
    if (typeof length === "number" && length <= 4096) {
      for (let i = 0; i < length; i++) {
        const pair = ownData(pairs, String(i));
        const key = ownData(pair, "0");
        // Native resources prepend incoming overrides and keep the first
        // non-null value. Never consult the SDK getter (or application hooks).
        if (typeof key === "string" && ownData(attributes, key) === undefined) {
          const value = ownData(pair, "1");
          if (value !== undefined && value !== null) (attributes as Record<string, unknown>)[key] = value;
        }
      }
    }
  }
  projected.resource = { attributes, schemaUrl: ownData(resource, "schemaUrl"), asyncAttributesPending: false };
  const snapshot = snapshotData(projected) as any;
  const identity = snapshot._spanContext;
  // Do not invoke an arbitrary spanContext()/TraceState.serialize(). Native SDK
  // spans carry these IDs in _spanContext; unsupported carriers get no fake ID.
  if (!identity || typeof identity !== "object" || typeof identity.traceId !== "string" || typeof identity.spanId !== "string") {
    throw new Error("Unsupported legacy span identity");
  }
  delete identity.traceState;
  if (snapshot.parentSpanContext && typeof snapshot.parentSpanContext === "object") delete snapshot.parentSpanContext.traceState;
  snapshot.spanContext = () => identity;
  snapshot.duration ??= [0, 0];
  snapshot.droppedAttributesCount ??= 0;
  snapshot.droppedEventsCount ??= 0;
  snapshot.droppedLinksCount ??= 0;
  return snapshot;
}

/** Replace a snapshot slot without invoking a setter or a Proxy trap. */
export function replaceOwnData(target: unknown, key: PropertyKey, value: unknown): boolean {
  if (!target || typeof target !== "object" || isProxy(target)) return false;
  const descriptor = getDescriptor(target, key);
  if (!descriptor || !("value" in descriptor) || !descriptor.writable) return false;
  defineProperty(target, key, { ...descriptor, value });
  return true;
}
