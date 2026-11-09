import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import { getRegisteredSecrets, markSecretDiscoveryFailed, registerSecret } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";
import { FISpan } from "../trace/trace-config/FISpan";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";

// RED-first source controls. No tests, builds or typechecks were run by this writer.
const savedEnv = { ...process.env };
const secretKey = Symbol.for("traceai.secrets");
let savedRegistry: PropertyDescriptor | undefined;
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  savedRegistry = Object.getOwnPropertyDescriptor(globalThis, secretKey);
  Reflect.deleteProperty(globalThis, secretKey);
});
afterEach(() => {
  jest.restoreAllMocks();
  process.env = { ...savedEnv };
  Reflect.deleteProperty(globalThis, secretKey);
  if (savedRegistry) Object.defineProperty(globalThis, secretKey, savedRegistry);
});
function fixture(attributes: Attributes = { note: "synthetic private prose" }): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "synthetic private name", attributes,
    kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    status: { code: 2, message: "synthetic private status" },
    resource: { attributes: { note: "synthetic resource" } },
    instrumentationScope: { name: "synthetic scope", attributes: { note: "synthetic scope note" } },
    events: [{ name: "synthetic event", time: [1, 2], attributes: { note: "synthetic event note" } }],
    links: [{ context: ctx, attributes: { note: "synthetic link note" } }] };
}
function expectMinimal(clean: any, hostLoss = false): void {
  expect(clean.attributes).toEqual(hostLoss ? { "traceai.capture.host_data_unknown": true } : {});
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]);
  expect(clean.links).toEqual([]);
  expect(clean.resource.attributes).toEqual({});
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>" });
  expect(clean.spanContext()).toEqual({ traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 });
  expect(clean.startTime).toEqual([1, 2]);
  expect(clean.endTime).toEqual([3, 4]);
  expect(clean.duration).toEqual([2, 2]);
}

test("R02 registry failure keeps status 2 and does not invent host loss", () => {
  Object.defineProperty(globalThis, secretKey, { configurable: true, value: {
    register() {}, values() { throw new Error("synthetic registry refusal"); },
  } });
  const raw = fixture(), before = JSON.stringify(raw);
  expectMinimal(privacyFilter().span(raw));
  expect(JSON.stringify(raw)).toBe(before);
});

test.each(["outgoing", "diagnostic", "retained-body"])("R02 %s discovery failure latch has no host-loss authority", route => {
  // Core-boundary control of the latch used by these routes; their actual
  // transport drivers remain in the protected traceai_auto suite run set.
  const filter = privacyFilter(), raw = fixture();
  expect(filter.span(raw).attributes.note).toBe("synthetic private prose");
  registerSecret(`synthetic-${route}-credential`);
  markSecretDiscoveryFailed();
  const before = JSON.stringify(raw);
  expectMinimal(filter.span(raw));
  expectMinimal(filter.span(raw));
  expect(JSON.stringify(raw)).toBe(before);
});

test.each(["cycle", "depth", "branch", "text"])("R03 sanitizer %s refusal preserves structural status without host loss", kind => {
  const cycle: any[] = []; cycle.push(cycle);
  let deep: any = "synthetic payload";
  for (let i = 0; i < 40; i++) deep = [deep];
  const payload = kind === "cycle" ? cycle : kind === "depth" ? deep
    : kind === "branch" ? Array(25000).fill("synthetic payload") : "synthetic payload".repeat(700000);
  const raw = fixture({ payload });
  expectMinimal(privacyFilter().span(raw));
  expect(raw.attributes.payload).toBe(payload);
  expect(raw.status).toEqual({ code: 2, message: "synthetic private status" });
});

test("R04 complete oversized JSON is FI discovery exhaustion, not host truncation", () => {
  const payload = JSON.stringify({ padding: "x".repeat(4194304), api_key: "synthetic-r6-large-key" });
  const raw = fixture({ payload });
  expectMinimal(privacyFilter().span(raw));
  expect(raw.attributes.payload).toBe(payload);
});

test.each(["string", "view", "array-buffer", "search-params"])("R05 null %s request discovery preserves status 2", _kind => {
  // captureSecrets(result) is null for all four protected transport controls.
  const raw = fixture(), before = JSON.stringify(raw);
  expectMinimal(privacyFilter().span(raw, null));
  expect(JSON.stringify(raw)).toBe(before);
});

test.each([0, 9])("C2 raw resource budget with native dropped count %i attributes loss correctly", drops => {
  const raw = fixture(); raw.droppedAttributesCount = drops;
  raw.resource = { _rawAttributes: Array.from({ length: 4097 }, (_, i) => [`safe.${i}`, "complete prose"]) };
  const before = JSON.stringify(raw), clean = privacyFilter().span(raw);
  expectMinimal(clean, drops > 0);
  expect(clean.droppedAttributesCount).toBe(drops);
  expect(JSON.stringify(raw)).toBe(before);
});

test("FI uncertainty stays sticky on later copied views without becoming host loss", () => {
  const raw = fixture({ payload: "synthetic payload".repeat(700000) });
  rememberSpanValueLimit(raw);
  const copy = { ...raw, attributes: { note: "small later copy" } };
  const clean = privacyFilter().span(copy);
  expectMinimal(clean);
  expectMinimal(privacyFilter().span(clean));
  expect(copy.attributes).toEqual({ note: "small later copy" });
  copy.droppedAttributesCount = 1;
  expectMinimal(privacyFilter().span(copy), true);
});

test.each(["resource-pairs", "events", "links"])("A-001 preparation refusal in %s still discovers a later independent credential", kind => {
  const raw = fixture(), secret = `synthetic-r6-later-${kind}-credential`;
  if (kind === "resource-pairs") raw.resource = { _rawAttributes: Array.from({ length: 4097 }, () => ["safe", "complete"]) };
  if (kind === "events") raw.events = Array.from({ length: 4097 }, () => ({ name: "complete", attributes: {} }));
  if (kind === "links") raw.links = Array.from({ length: 4097 }, () => ({ context: raw._spanContext, attributes: {} }));
  const extra = { api_key: secret }, before = JSON.stringify([raw, extra]);
  expect(getRegisteredSecrets()).not.toContain(secret);
  expectMinimal(privacyFilter().span(raw, [], extra));
  expect(getRegisteredSecrets()).toContain(secret);
  expect(JSON.stringify([raw, extra])).toBe(before);
});

test.each(["throw", "duplicate", "non-key", "non-array"])("OWNKEYS ordinary event map %s return withholds every surface", kind => {
  const raw = fixture(), target = raw.events[0].attributes, ownKeys = Reflect.ownKeys;
  const spy = jest.spyOn(Reflect, "ownKeys").mockImplementation(value => {
    if (value !== target) return ownKeys(value);
    if (kind === "throw") throw new Error("synthetic ownKeys refusal");
    if (kind === "duplicate") return ["note", "note"];
    if (kind === "non-key") return [17] as unknown as (string | symbol)[];
    return {} as (string | symbol)[];
  });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expectMinimal(clean);
  expect(raw.events[0].attributes).toEqual({ note: "synthetic event note" });
});

test("OWNKEYS proxy map is refused without executing its trap or exporting other content", () => {
  const trap = jest.fn(() => { throw new Error("synthetic proxy trap"); });
  const raw = fixture(); raw.events[0].attributes = new Proxy({}, { ownKeys: trap });
  expectMinimal(privacyFilter().span(raw));
  expect(trap).not.toHaveBeenCalled();
});

test.each([false, true])("END final setter failure closes one native span (auto=%s)", async auto => {
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const native = provider.getTracer("r6-end").startSpan("synthetic operation");
    native.setStatus({ code: 2, message: "synthetic native status" });
    native.setAttribute("app.note", "synthetic native application data");
    const proxy = new Proxy(native, { get(target, key) {
      if (["setAttribute", "setAttributes", "setStatus"].includes(String(key))) return () => { throw new Error("synthetic setter failure"); };
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    } });
    const wrapped = new FISpan({ span: proxy, config: generateTraceConfig(), autoDestination: auto });
    expect(() => wrapped.setStatus({ code: 2 })).toThrow("synthetic setter failure");
    expect(() => wrapped.end()).not.toThrow();
    expect(() => wrapped.end()).not.toThrow();
    expect(memory.getFinishedSpans()).toHaveLength(1);
    const ended = memory.getFinishedSpans()[0], clean = privacyFilter().span(ended);
    expect(clean.attributes).toEqual({});
    expect(clean.status).toEqual({ code: 2 });
    expect(clean.events).toEqual([]);
    expect(clean.links).toEqual([]);
    expect(clean.name).toBe("<unknown: host-truncated>");
    if (auto) expect(ended.attributes["app.note"]).toBe("synthetic native application data");
  } finally { await provider.shutdown(); }
});

test("END standalone admit({}) failure without error status still closes the span", async () => {
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    const native = provider.getTracer("r6-admit").startSpan("synthetic operation");
    const proxy = new Proxy(native, { get(target, key) {
      if (key === "setAttributes") return () => { throw new Error("synthetic admission setter"); };
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    } });
    const wrapped = new FISpan({ span: proxy, config: generateTraceConfig() });
    expect(() => wrapped.end()).not.toThrow();
    expect(memory.getFinishedSpans()).toHaveLength(1);
    expect(privacyFilter().span(memory.getFinishedSpans()[0]).attributes).toEqual({});
  } finally { await provider.shutdown(); }
});

test.each(["start", "updates"])("AUTO count includes all 3 FI redactions at %s; manual adds no count", async schedule => {
  registerSecret("synthetic-r6-count-credential");
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  try {
    // Exercise the manual negative even while the auto accounting control is RED.
    for (const auto of [false, true]) {
      const tracer = new FITracer({ tracer: (auto ? FITracer.autoProvider(provider) : provider).getTracer("r6-count") });
      const attrs = { first: "synthetic-r6-count-credential", second: "synthetic-r6-count-credential", third: "synthetic-r6-count-credential" };
      const span = tracer.startSpan("synthetic count", { attributes: schedule === "start" ? attrs : {} });
      if (schedule === "updates") for (const [key, value] of Object.entries(attrs)) span.setAttribute(key, value);
      span.end();
      const native = memory.getFinishedSpans().at(-1)!, before = JSON.stringify(native.attributes);
      const clean = privacyFilter().span(native);
      expect(clean.attributes.first).toBe("<redacted>");
      expect(clean.attributes.second).toBe("<redacted>");
      expect(clean.attributes.third).toBe("<redacted>");
      if (auto) expect(clean.attributes["traceai.capture.dropped_content_count"]).toBe(3);
      else expect(clean.attributes["traceai.capture.dropped_content_count"]).toBeUndefined();
      expect(clean.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
      expect(JSON.stringify(native.attributes)).toBe(before);
    }
  } finally { await provider.shutdown(); }
});
