import { type Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import * as runtime from "../privacy-runtime";
import { registerSecret } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// RED-first source controls. Neither RED nor GREEN has been executed.
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
const scanLimit = 4 * 1024 * 1024;
function fixture(attributes: Attributes = {}): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, attributes, name: "native operation name", kind: 2,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true, status: { code: 2 },
    resource: { attributes: {} }, instrumentationScope: { name: "native scope name", version: "long version", schemaUrl: "https://example.invalid/schema" }, events: [], links: [] };
}
function copied(native: any, note: string): any {
  return { ...native, attributes: { note, "gen_ai.usage.total_tokens": 7 }, status: { code: 2, message: note },
    resource: { attributes: { note } }, instrumentationScope: { ...native.instrumentationScope, attributes: { note } },
    events: [{ name: "native event name", time: [1, 0], attributes: { "exception.message": note, "exception.stacktrace": note } }],
    links: [{ context: native.spanContext(), attributes: { note } }] };
}
function expectWithheld(clean: any): void {
  expect(clean.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.status.message).toBe("<unknown: host-truncated>");
  expect(clean.resource.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.events[0].attributes["exception.message"]).toBe("<unknown: host-truncated>");
  expect(clean.events[0].attributes["exception.stacktrace"]).toBe("<unknown: host-truncated>");
  expect(clean.links[0].attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe(7);
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.events[0].name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: { note: "<unknown: host-truncated>" } });
  expect(clean.attributes).toEqual({ note: "<unknown: host-truncated>", "gen_ai.usage.total_tokens": 7, "traceai.capture.host_data_unknown": true });
  expect(clean.status).toEqual({ code: 2, message: "<unknown: host-truncated>" });
  expect(clean.resource.attributes).toEqual({ note: "<unknown: host-truncated>" });
  expect(clean.events).toHaveLength(1); expect(clean.links).toHaveLength(1);
  expect(clean.events[0].attributes).toEqual({ "exception.message": "<unknown: host-truncated>", "exception.stacktrace": "<unknown: host-truncated>" });
  expect(clean.links[0].attributes).toEqual({ note: "<unknown: host-truncated>" });
}

test.each(["direct", "spanLimits", "absent"].flatMap(slot => [false, true].map(hook => ({ slot, hook }))))(
  "R3 boundary-only observation is sticky with $slot copied slots and hook=$hook", ({ slot, hook }) => {
    const native = fixture(); native._attributeValueLengthLimit = 8;
    if (hook) rememberSpanValueLimit(native); // Before, not after, truncation occurs.
    native.attributes.note = "cut/v1::";
    const before = JSON.stringify(native), filter = privacyFilter({}, ["cut/v1::rest-of-synthetic-credential"]);
    const first = filter.span(native);
    expect(first.attributes).toEqual({ note: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
    expect(first.name).toBe("<unknown: host-truncated>");
    expect(first.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    expect(first.resource.attributes).toEqual({}); expect(first.status).toEqual({ code: 2 });
    expect(first.events).toEqual([]); expect(first.links).toEqual([]);
    expect(first.spanContext()).toEqual(native.spanContext());
    const mapped = copied(native, "cut/v1:");
    if (slot !== "direct") delete mapped._attributeValueLengthLimit;
    if (slot === "spanLimits") mapped._spanLimits = { attributeValueLengthLimit: 8 };
    if (hook) rememberSpanValueLimit(mapped);
    const mappedBefore = JSON.stringify(mapped);
    expectWithheld(filter.span(mapped));
    expectWithheld(privacyFilter().span(mapped)); // Separate boundary instance, same weak identity.
    expect(JSON.stringify(native)).toBe(before);
    expect(JSON.stringify(mapped)).toBe(mappedBefore);
  });

test.each(["attributes", "status", "resource", "rawResource", "scope", "event", "link"])(
  "R3 current %s sentinel withholds shorter siblings before discovery", surface => {
    registerSecret("cut/v1::rest-of-synthetic-credential");
    const raw = copied(fixture(), "cut/v1:"); raw._attributeValueLengthLimit = 8;
    if (surface === "attributes") raw.attributes.sentinel = "cut/v1::";
    if (surface === "status") raw.status.message = "cut/v1::";
    if (surface === "resource") raw.resource.attributes.sentinel = "cut/v1::";
    if (surface === "rawResource") raw.resource = { _rawAttributes: [["sentinel", "cut/v1::"]] };
    if (surface === "scope") raw.instrumentationScope.attributes.sentinel = "cut/v1::";
    if (surface === "event") raw.events[0].attributes.sentinel = "cut/v1::";
    if (surface === "link") raw.links[0].attributes.sentinel = "cut/v1::";
    const before = JSON.stringify(raw), clean = privacyFilter().span(raw);
    expect(clean.attributes.note).toBe("<unknown: host-truncated>");
    expect(clean.status).toEqual({ code: 2, message: "<unknown: host-truncated>" });
    expect([clean.name, clean.instrumentationScope.name, clean.instrumentationScope.version, clean.instrumentationScope.schemaUrl, clean.events[0].name]).toEqual(Array(5).fill("<unknown: host-truncated>"));
    expect(clean.events).toHaveLength(1); expect(clean.links).toHaveLength(1);
    expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe(7);
    for (const values of [clean.attributes, clean.resource.attributes, (clean.instrumentationScope as any).attributes, clean.events[0].attributes!, clean.links[0].attributes!]) for (const [key, value] of Object.entries(values)) if (key !== "gen_ai.usage.total_tokens" && key !== "traceai.capture.host_data_unknown") expect(value).toBe(key === "session.id" ? "<redacted>" : "<unknown: host-truncated>");
    expect(clean.spanContext()).toEqual(raw.spanContext());
    expect(clean.events[0].attributes?.["exception.message"]).toBe("<unknown: host-truncated>");
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(JSON.stringify(clean)).not.toContain("cut/v1:");
    expect(JSON.stringify(raw)).toBe(before);
  });

test("R3 boundary count loss persists without inventing truncation or recovering dropped data", () => {
  const raw = fixture({ note: "ordinary prose" }); raw.droppedAttributesCount = 1;
  const first = privacyFilter().span(raw);
  expect(first.attributes).toEqual({ note: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
  expect(first.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  const mapped = { ...raw, droppedAttributesCount: 0 };
  const clean = privacyFilter().span(mapped);
  expect(clean.attributes).toEqual({ note: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
  for (const view of [first, clean]) {
    expect(view.name).toBe("<unknown: host-truncated>");
    expect(view.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    expect(view.resource.attributes).toEqual({}); expect(view.status).toEqual({ code: 2 });
    expect(view.events).toEqual([]); expect(view.links).toEqual([]);
    expect(view.spanContext()).toEqual(raw.spanContext());
  }
  expect(first.droppedAttributesCount).toBe(1);
  expect(clean.droppedAttributesCount).toBe(0);
  expect(raw.droppedAttributesCount).toBe(1);
});

test("R3 marker probes share one allowance across many attributes, secrets and all surfaces", () => {
  const secrets = Array.from({ length: 1024 }, (_, i) => `s${i.toString(16).padStart(7, "0")}-synthetic-budget-secret`);
  const chunk = "q".repeat(4096), raw = copied(fixture(), chunk);
  raw._attributeValueLengthLimit = 8;
  raw.attributes = { ...Object.fromEntries(Array.from({ length: 512 }, (_, i) => [`note${i}`, chunk])), "session.id": chunk };
  const filter = privacyFilter({}, secrets), before = JSON.stringify(raw), includes = String.prototype.includes;
  let probes = 0, chars = 0;
  const spy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) {
    if (String(this) === chunk) {
      probes++; chars += chunk.length;
      // Stop the defective base promptly; the recorded overrun still fails below.
      if (chars > scanLimit) throw new Error("R3 observed marker budget overrun");
    }
    return includes.call(this, search, position);
  });
  let clean: any;
  try { clean = filter.span(raw); } finally { spy.mockRestore(); }
  expect(probes).toBe(0);
  expect(chars).toBeLessThanOrEqual(scanLimit);
  expect(clean.attributes.note0).toBe("<unknown: host-truncated>");
  expect(clean.attributes.note511).toBe("<unknown: host-truncated>");
  expect(clean.attributes["session.id"]).toBe("<redacted>");
  expect(clean.status.message).toBe("<unknown: host-truncated>");
  expect(clean.resource.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.events[0].attributes["exception.message"]).toBe("<unknown: host-truncated>");
  expect(clean.links[0].attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(JSON.stringify(raw)).toBe(before);
});

test.each([false, true])("R3 many exempt-key events and links share discovery work (aliased=%s)", aliased => {
  const chunk = "ordinary prose! ".repeat(4096), shared = { "gen_ai.operation.name": chunk };
  const raw = fixture();
  raw.events = Array.from({ length: 128 }, () => ({ name: "event", attributes: aliased ? shared : { ...shared } }));
  raw.links = Array.from({ length: 128 }, () => ({ context: raw.spanContext(), attributes: aliased ? shared : { ...shared } }));
  const discover = runtime.discoverSecrets, before = JSON.stringify(raw);
  let chars = 0, calls = 0;
  const spy = jest.spyOn(runtime, "discoverSecrets").mockImplementation((sources, entries) => {
    const materialized = Array.from(sources), seen = new Set<object>(); calls++;
    const visit = (value: unknown): void => {
      if (typeof value === "string") { if (value === chunk) chars += value.length; return; }
      if (value && typeof value === "object" && !seen.has(value)) {
        seen.add(value); for (const item of Object.values(value)) visit(item);
      }
    };
    materialized.forEach(visit);
    if (chars > scanLimit) throw new Error("R3 observed discovery budget overrun");
    return discover(materialized, entries);
  });
  let clean: any;
  try {
    // Populated control proves this observer sees the real discovery call.
    expect(privacyFilter().span(fixture({ note: "ordinary prose" })).attributes.note).toBe("ordinary prose");
    expect(calls).toBeGreaterThan(0); chars = 0; calls = 0;
    clean = privacyFilter().span(raw);
  } finally { spy.mockRestore(); }
  expect(chars).toBeLessThanOrEqual(scanLimit);
  expect(calls).toBeLessThanOrEqual(1);
  expect(clean.attributes).toEqual({});
  expect(clean.events).toEqual([]);
  const mapped = { ...raw, attributes: { note: "short copy", "session.id": "short" }, events: [], links: [] };
  const next = privacyFilter().span(mapped);
  expect(next.attributes).toEqual({});
  expect(next.name).toBe("<unknown: host-truncated>");
  expect(next.status).toEqual({ code: 2 });
  expect(JSON.stringify(raw)).toBe(before);
});

test("R3 loss suffix and marker probes debit the same export allowance", () => {
  const chunk = "ordinary! ".repeat(10000), raw = fixture({ note: chunk });
  raw.events = Array.from({ length: 32 }, () => ({ name: "event", attributes: { note: chunk } }));
  raw.links = [{ context: raw.spanContext(), attributes: { note: "tiny [truncated]" } }];
  const filter = privacyFilter({}, ["absent-r3-synthetic-secret"]), test = RegExp.prototype.test, includes = String.prototype.includes;
  let chars = 0;
  const charge = (value: string): void => { if (value === chunk) { chars += value.length; if (chars > scanLimit) throw new Error("R3 observed aggregate overrun"); } };
  const regexSpy = jest.spyOn(RegExp.prototype, "test").mockImplementation(function (this: RegExp, value: string) { charge(value); return test.call(this, value); });
  const prefixSpy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) { charge(String(this)); return includes.call(this, search, position); });
  let clean: any;
  try { clean = filter.span(raw); } finally { regexSpy.mockRestore(); prefixSpy.mockRestore(); }
  expect(chars).toBeGreaterThan(0); expect(chars).toBeLessThanOrEqual(scanLimit);
  expect(clean.attributes).toEqual({ "traceai.capture.host_data_unknown": true });
  expect(clean.events).toEqual([]);
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(raw.attributes.note).toBe(chunk);
});

test("Q7b application status and Error stay byte-identical while known FI-authored status is masked", async () => {
  const secret = "q7-r3-synthetic-status-secret", message = `retry ${secret} now`;
  registerSecret(secret);
  const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const applicationError = new Error(message), errorBefore = Object.getOwnPropertyDescriptors(applicationError);
    const application = provider.getTracer("application").startSpan("application");
    application.setStatus({ code: 2, message }); application.recordException(applicationError); application.end();
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("authored") }).startSpan("authored");
    fi.setStatus({ code: 2, message }); fi.recordException(applicationError); fi.end();
    const [nativeApplication, nativeFI] = exporter.getFinishedSpans();
    const applicationBefore = JSON.stringify([nativeApplication.status, nativeApplication.events]);
    expect(nativeApplication.status.message).toBe(message);
    expect(nativeApplication.events[0].attributes?.["exception.message"]).toBe(message);
    expect(nativeFI.status.message).toBe("retry <redacted> now");
    expect(privacyFilter().span(nativeApplication).status.message).toBe("retry <redacted> now");
    expect(privacyFilter().span(nativeFI).status.message).toBe("retry <redacted> now");
    expect(JSON.stringify([nativeApplication.status, nativeApplication.events])).toBe(applicationBefore);
    expect(Object.getOwnPropertyDescriptors(applicationError)).toEqual(errorBefore);
    expect(applicationError.message).toBe(message);
  } finally { await provider.shutdown(); }
});
