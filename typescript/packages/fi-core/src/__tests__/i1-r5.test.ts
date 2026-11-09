import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { FITracer } from "../trace/trace-config/FITracer";
import { privacyFilter } from "../export-privacy";
import { getRegisteredSecrets } from "../secrets";

// RED-first source controls; neither RED nor GREEN has been executed.
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
function fixture(attributes: Attributes = { note: "ordinary prose" }): any {
  const ctx = { traceId: "3".repeat(32), spanId: "4".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "span prose", attributes, kind: 2,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true,
    status: { code: 2, message: "status prose" }, resource: { attributes: { note: "resource prose" } },
    instrumentationScope: { name: "scope prose", version: "version prose", schemaUrl: "https://example.invalid/schema", attributes: { note: "scope attribute" } },
    events: [{ name: "event prose", time: [1, 0], attributes: { note: "event attribute" } }],
    links: [{ context: ctx, attributes: { note: "link attribute" } }] };
}
function expectMinimal(clean: any, hostLoss = true): void {
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.attributes).toEqual(hostLoss ? { "traceai.capture.host_data_unknown": true } : {});
  expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]);
  expect(clean.links).toEqual([]);
  expect(clean.resource.attributes).toEqual({});
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>" });
}

test("Q9 auto FI writes within the native count limit retain content without FI-created loss flags", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 16 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("r5-auto") }).startSpan("ordinary span", { attributes: { "input.value": "ordinary input" } });
    fi.setAttributes({ "output.value": "ordinary answer", "gen_ai.usage.input_tokens.audio": undefined });
    fi.end();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const native = exporter.getFinishedSpans()[0], before = JSON.stringify(native.attributes);
    expect(native.droppedAttributesCount).toBe(0);
    expect(native.attributes).toEqual({ "input.value": "ordinary input", "output.value": "ordinary answer" });
    expect(native.attributes["traceai.wire.incomplete"]).toBeUndefined();
    expect(native.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
    const clean = privacyFilter().span(native);
    expect(clean.attributes).toEqual({ "input.value": "ordinary input", "output.value": "ordinary answer" });
    expect(clean.name).toBe("ordinary span");
    expect(clean.attributes["traceai.wire.incomplete"]).toBeUndefined();
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test("Q9 observed native count loss blankets every FI content surface without projecting or mutating the host", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ resource: resourceFromAttributes({ note: "resource prose" }), spanLimits: { attributeCountLimit: 1 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("scope prose", "version prose") }).startSpan("span prose", { attributes: { "input.value": "ordinary input" } });
    const native: any = fi.getContextSpan();
    native.setStatus({ code: 2, message: "status prose" });
    native.addEvent("event prose", { note: "event attribute" });
    native.addLink({ context: native.spanContext(), attributes: { note: "link attribute" } });
    fi.setAttribute("output.value", "ordinary answer"); fi.end();
    const ended = exporter.getFinishedSpans()[0];
    const before = JSON.stringify([ended.attributes, ended.status, ended.events, ended.links]);
    expect(ended.droppedAttributesCount).toBe(1);
    expect(ended.attributes).toEqual({ "input.value": "ordinary input" });
    expect(ended.attributes["traceai.wire.incomplete"]).toBeUndefined();
    const clean = privacyFilter().span(ended, [], { extra: "overlay prose" });
    expect(clean.attributes).toEqual({ "input.value": "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
    expect(clean.name).toBe("<unknown: host-truncated>");
    expect(clean.status).toEqual({ code: 2, message: "<unknown: host-truncated>" });
    expect(clean.resource.attributes).toEqual({ note: "<unknown: host-truncated>", extra: "<unknown: host-truncated>" });
    expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    expect(clean.events[0].name).toBe("<unknown: host-truncated>");
    expect(clean.events[0].attributes).toEqual({ note: "<unknown: host-truncated>" });
    expect(clean.links[0].attributes).toEqual({ note: "<unknown: host-truncated>" });
    expect(clean.attributes["traceai.wire.incomplete"]).toBeUndefined();
    expect(clean.droppedAttributesCount).toBe(1);
    expect(JSON.stringify([ended.attributes, ended.status, ended.events, ended.links])).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["resource", "scope", "event", "link", "status", "name", "extra"])("A-001 structural inspection refusal still discovers a later %s credential", surface => {
  const raw = fixture(Object.fromEntries(Array.from({ length: 5000 }, (_, i) => [`k${i}`, "x"])));
  raw.droppedAttributesCount = 1;
  const secret = `r5-${surface}-synthetic-violet-pear`, json = JSON.stringify({ api_key: secret });
  const extra: Attributes = {};
  if (surface === "resource") raw.resource.attributes.api_key = secret;
  if (surface === "scope") raw.instrumentationScope.attributes.api_key = secret;
  if (surface === "event") raw.events[0].attributes.api_key = secret;
  if (surface === "link") raw.links[0].attributes.api_key = secret;
  if (surface === "status") raw.status.message = json;
  if (surface === "name") raw.name = json;
  if (surface === "extra") extra.api_key = secret;
  expect(getRegisteredSecrets()).not.toContain(secret);
  const before = JSON.stringify([raw, extra]);
  const clean = privacyFilter().span(raw, [], extra);
  expect(getRegisteredSecrets()).toContain(secret);
  expectMinimal(clean);
  expect(JSON.stringify([raw, extra])).toBe(before);
});

test("A-001 discovery refusal on one surface cannot bypass a later credential while allowance remains", () => {
  const raw = fixture(); raw.droppedAttributesCount = 1;
  raw.resource.attributes = Object.fromEntries(Array.from({ length: 5000 }, (_, i) => [`r${i}`, "x"]));
  raw.links[0].attributes.api_key = "r5-later-link-synthetic-violet-pear";
  const clean = privacyFilter().span(raw);
  expect(getRegisteredSecrets()).toContain("r5-later-link-synthetic-violet-pear");
  expectMinimal(clean);
});

test("A-001 whole-ledger exhaustion stays minimal and does not claim discovery of an unread later surface", () => {
  const raw = fixture({ note: "ordinary prose ".repeat(400000) }); raw.droppedAttributesCount = 1;
  raw.resource.attributes.api_key = "r5-unread-synthetic-violet-pear";
  expect(getRegisteredSecrets()).not.toContain("r5-unread-synthetic-violet-pear");
  expectMinimal(privacyFilter().span(raw));
  expect(getRegisteredSecrets()).not.toContain("r5-unread-synthetic-violet-pear");
});

test("B-001 hidden own keys are charged and refused; failed snapshots are not enumerated again", () => {
  const attrs = {};
  for (let i = 0; i < 5000; i++) Object.defineProperty(attrs, `hidden${i}`, { value: "hidden prose", enumerable: false });
  const raw = fixture(attrs), ownKeys = Reflect.ownKeys;
  let snapshots = 0;
  const spy = jest.spyOn(Reflect, "ownKeys").mockImplementation(value => { if (value === attrs) snapshots++; return ownKeys(value); });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expect(snapshots).toBe(1);
  expectMinimal(clean, false);
});

test("B-001 aggregate hidden-key snapshots consume one ledger across distinct event and link maps", () => {
  const maps = Array.from({ length: 128 }, () => {
    const attrs = {};
    for (let i = 0; i < 256; i++) Object.defineProperty(attrs, `hidden${i}`, { value: "hidden prose", enumerable: false });
    return attrs;
  });
  const raw = fixture();
  raw.events = maps.slice(0, 64).map(attributes => ({ name: "event", attributes }));
  raw.links = maps.slice(64).map(attributes => ({ context: raw._spanContext, attributes }));
  const ownKeys = Reflect.ownKeys; let snapshots = 0;
  const spy = jest.spyOn(Reflect, "ownKeys").mockImplementation(value => { if (maps.includes(value)) snapshots++; return ownKeys(value); });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expect(snapshots).toBeGreaterThan(0);
  expect(snapshots).toBeLessThan(128);
  expectMinimal(clean, false);
});

test("B-001 a bounded aliased hidden-key map is snapshotted once without charging every alias as a new map", () => {
  const attrs = {};
  for (let i = 0; i < 64; i++) Object.defineProperty(attrs, `hidden${i}`, { value: "hidden prose", enumerable: false });
  const raw = fixture(); raw.events = Array.from({ length: 64 }, () => ({ name: "event", attributes: attrs }));
  raw.links = Array.from({ length: 64 }, () => ({ context: raw._spanContext, attributes: attrs }));
  const ownKeys = Reflect.ownKeys; let snapshots = 0;
  const spy = jest.spyOn(Reflect, "ownKeys").mockImplementation(value => { if (value === attrs) snapshots++; return ownKeys(value); });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expect(snapshots).toBe(1);
  expect(clean.attributes).toEqual({ note: "ordinary prose" });
  expect(clean.events).toHaveLength(64);
  expect(clean.links).toHaveLength(64);
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
});
