import type { Attributes } from "@opentelemetry/api";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import { getRegisteredSecrets } from "../secrets";
import * as runtime from "../privacy-runtime";

// Synthetic RED-first source controls. RED/GREEN have not been executed.
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
function fixture(attributes: Attributes = { note: "short prose" }): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, attributes, name: "span prose", kind: 2,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true,
    status: { code: 2, message: "status prose" }, resource: { attributes: { note: "resource prose" } },
    instrumentationScope: { name: "scope prose", version: "version prose", schemaUrl: "https://example.invalid/schema", attributes: { note: "scope attribute" } },
    events: [{ name: "event prose", time: [1, 0], attributes: { note: "event attribute", "exception.message": "exception prose" } }],
    links: [{ context: ctx, attributes: { note: "link attribute" } }] };
}
function expectBlanket(clean: any): void {
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.status).toEqual({ code: 2, message: "<unknown: host-truncated>" });
  expect(clean.resource.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: { note: "<unknown: host-truncated>" } });
  expect(clean.events[0]).toEqual({ name: "<unknown: host-truncated>", time: [1, 0], attributes: { note: "<unknown: host-truncated>", "exception.message": "<unknown: host-truncated>" }, droppedAttributesCount: 0 });
  expect(clean.links[0].attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
}

test.each(["attribute-count", "event-count", "link-count", "event-attribute-count", "link-attribute-count", "resource-count", "scope-count", "wire", "wire-incomplete", "wire-unavailable", "at-limit", "status-marker", "resource-marker", "scope-marker", "event-marker", "link-marker", "overlay-marker"])(
  "Q8b %s host loss blankets every surface and a colliding resource overlay", trigger => {
    const raw = fixture({ note: "short prose", "session.id": "synthetic session", "gen_ai.usage.total_tokens": 7 });
    const extra: Attributes = { note: "overlay prose" };
    if (trigger === "attribute-count") raw.droppedAttributesCount = 1;
    if (trigger === "event-count") raw._droppedEventsCount = 1;
    if (trigger === "link-count") raw._droppedLinksCount = 1;
    if (trigger === "event-attribute-count") raw.events[0].droppedAttributesCount = 1;
    if (trigger === "link-attribute-count") raw.links[0].droppedAttributesCount = 1;
    if (trigger === "resource-count") raw.resource.droppedAttributesCount = 1;
    if (trigger === "scope-count") raw.instrumentationScope.droppedAttributesCount = 1;
    if (trigger === "wire") raw.attributes["traceai.wire.truncated"] = true;
    if (trigger === "wire-incomplete") raw.attributes["traceai.wire.incomplete"] = true;
    if (trigger === "wire-unavailable") raw.attributes["traceai.wire.body_unavailable"] = true;
    if (trigger === "at-limit") { raw.attributes.sentinel = "12345678"; raw._attributeValueLengthLimit = 8; }
    if (trigger === "status-marker") raw.status.message = "tiny [truncated]";
    if (trigger === "resource-marker") raw.resource.attributes.sentinel = "tiny [truncated]";
    if (trigger === "scope-marker") raw.instrumentationScope.attributes.sentinel = "tiny [truncated]";
    if (trigger === "event-marker") raw.events[0].attributes.sentinel = "tiny [truncated]";
    if (trigger === "link-marker") raw.links[0].attributes.sentinel = "tiny [truncated]";
    if (trigger === "overlay-marker") extra.sentinel = "tiny [truncated]";
    const before = JSON.stringify([raw, extra]), clean = privacyFilter().span(raw, [], extra);
    // Remove sentinel keys only from this detached test copy for the common literal oracle.
    const checked: any = { ...clean, instrumentationScope: { ...clean.instrumentationScope, attributes: { note: (clean.instrumentationScope as typeof clean.instrumentationScope & { attributes?: Attributes }).attributes?.note } }, events: clean.events.map(e => ({ ...e, attributes: { note: e.attributes?.note, "exception.message": e.attributes?.["exception.message"] }, droppedAttributesCount: 0 })) };
    expectBlanket(checked);
    expect(clean.attributes["session.id"]).toBe("<redacted>");
    expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe(7);
    expect(JSON.stringify(clean)).not.toContain("overlay prose");
    expect(JSON.stringify([raw, extra])).toBe(before);
  });

test.each(["direct", "nested", "absent"])("Q8b native -> detached -> shortened mapped copy -> later view (%s limits)", slot => {
  const raw = fixture(); raw._attributeValueLengthLimit = 8; raw.attributes.note = "cut/v1::";
  const filter = privacyFilter({}, ["cut/v1::rest-of-synthetic-credential"]);
  const first = filter.span(raw);
  expect(first.attributes.note).toBe("<unknown: host-truncated>");
  const mapped = { ...fixture(), spanContext: first.spanContext, attributes: { note: "cut/v1:", "session.id": "short" } };
  delete mapped._spanContext;
  if (slot === "direct") mapped._attributeValueLengthLimit = 8;
  if (slot === "nested") mapped._spanLimits = { attributeValueLengthLimit: 8 };
  const before = JSON.stringify(mapped);
  const second = privacyFilter().span(mapped, [], { note: "cut/v1:" });
  expectBlanket(second);
  expectBlanket(privacyFilter().span(second, [], { note: "new overlay" }));
  expect(second.attributes["session.id"]).toBe("<redacted>");
  expect(JSON.stringify(second)).not.toContain("cut/v1:");
  expect(JSON.stringify(mapped)).toBe(before);
});

test("Q8b views created before an observation share future loss; exported values are not retracted", () => {
  const raw = fixture(), filter = privacyFilter();
  const beforeLoss = filter.span(raw);
  expect(beforeLoss.attributes.note).toBe("short prose");
  raw.droppedAttributesCount = 1;
  rememberSpanValueLimit(raw);
  expectBlanket(filter.span(beforeLoss));
  expectBlanket(filter.span(raw));
  expect(beforeLoss.attributes.note).toBe("short prose"); // Explicit residual: no retroactive mutation.
});

test("Q8b no-loss positive retains complete content and uses detached objects", () => {
  const raw = fixture({ note: "ordinary words", "gen_ai.usage.total_tokens": 7 }), before = JSON.stringify(raw);
  const clean = privacyFilter().span(raw, [], { extra: "extra prose" });
  expect(clean.attributes).toEqual({ note: "ordinary words", "gen_ai.usage.total_tokens": 7 });
  expect(clean.name).toBe("span prose");
  expect(clean.status).toEqual({ code: 2, message: "status prose" });
  expect(clean.resource.attributes).toEqual({ note: "resource prose", extra: "extra prose" });
  expect(clean.instrumentationScope.name).toBe("scope prose");
  expect(clean.events[0].name).toBe("event prose");
  expect(clean.links[0].attributes?.note).toBe("link attribute");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
  expect(clean.attributes).not.toBe(raw.attributes);
  expect(JSON.stringify(raw)).toBe(before);
});

test.each(["resource", "scope", "event", "link", "status", "name", "extra"])("B-001 discovers a new synthetic credential on withheld %s before output", surface => {
  const secret = `r4-${surface}-synthetic-purple-plum`, json = JSON.stringify({ api_key: secret });
  const raw = fixture(); raw.droppedAttributesCount = 1;
  const extra: Attributes = {};
  if (surface === "resource") raw.resource.attributes.note = json;
  if (surface === "scope") raw.instrumentationScope.attributes.note = json;
  if (surface === "event") raw.events[0].attributes.note = json;
  if (surface === "link") raw.links[0].attributes.note = json;
  if (surface === "status") raw.status.message = json;
  if (surface === "name") raw.name = json;
  if (surface === "extra") extra.note = json;
  const clean = privacyFilter().span(raw, [], extra);
  expect(getRegisteredSecrets()).toContain(secret);
  expectBlanket(clean);
  expect(JSON.stringify(clean)).not.toContain(secret);
});

test("Q8b NON_CONTENT validates types and finite enums, never arbitrary text or metric prefixes", () => {
  const raw = fixture({ note: "tiny [truncated]", "gen_ai.operation.name": '{"api_key":"r4-enum-synthetic-secret"}', "gen_ai.span.kind": "arbitrary prose", "gen_ai.usage.total_tokens": "host prose", "gen_ai.usage.input_tokens": 7, "gen_ai.usage.user_payload": 9, "traceai.wire.incomplete": true });
  const clean = privacyFilter().span(raw);
  expect(clean.attributes).toEqual({ note: "<unknown: host-truncated>", "gen_ai.operation.name": "<unknown: host-truncated>", "gen_ai.span.kind": "<unknown: host-truncated>", "gen_ai.usage.total_tokens": "<unknown: host-truncated>", "gen_ai.usage.input_tokens": 7, "gen_ai.usage.user_payload": "<unknown: host-truncated>", "traceai.wire.incomplete": true, "traceai.capture.host_data_unknown": true });
  expect(getRegisteredSecrets()).toContain("r4-enum-synthetic-secret");
});

test.skip("A7 private complete-value omissions (withdrawn: fi-security Q9 Candidate 2, no auto-mode projection)", () => {
  const raw = fixture({ note: "ordinary words", "traceai.capture.dropped_content_count": 3 });
  const first = privacyFilter().span(raw);
  expect(first.attributes).toEqual({ note: "ordinary words", "traceai.capture.dropped_content_count": 3 });
  expect(first.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
  const second = privacyFilter().span(first);
  expect(second.attributes).toEqual({ note: "ordinary words", "traceai.capture.dropped_content_count": 3 });
  raw.droppedAttributesCount = 1;
  const lost = privacyFilter().span(raw);
  expect(lost.attributes.note).toBe("<unknown: host-truncated>");
  expect(lost.attributes["traceai.capture.dropped_content_count"]).toBe(3);
  expect(lost.attributes["traceai.capture.host_data_unknown"]).toBe(true);
});

test("B-002 accessor-only maps debit probes and aliases reuse bounded descriptor snapshots", () => {
  const execute = jest.fn(() => { throw new Error("must not invoke"); }), attrs = {};
  for (let i = 0; i < 64; i++) Object.defineProperty(attrs, `accessor${i}`, { enumerable: true, get: execute });
  const raw = fixture(); raw.events = Array.from({ length: 64 }, () => ({ name: "event", attributes: attrs }));
  raw.links = Array.from({ length: 64 }, () => ({ context: raw._spanContext, attributes: attrs }));
  const descriptor = Object.getOwnPropertyDescriptor; let probes = 0;
  const spy = jest.spyOn(Object, "getOwnPropertyDescriptor").mockImplementation((value, key) => { if (value === attrs && String(key).startsWith("accessor")) probes++; return descriptor(value, key); });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expect(probes).toBe(64);
  expect(execute).not.toHaveBeenCalled();
  expect(clean.attributes.note).toBe("short prose");
  expect(clean.events).toHaveLength(64);
});

test("B-002 rejects the 4097th inspected accessor key without probing its descriptor", () => {
  const attrs = {}, execute = jest.fn();
  for (let i = 0; i < 5000; i++) Object.defineProperty(attrs, `accessor${i}`, { enumerable: true, get: execute });
  const raw = fixture(attrs), descriptor = Object.getOwnPropertyDescriptor; let probes = 0;
  const spy = jest.spyOn(Object, "getOwnPropertyDescriptor").mockImplementation((value, key) => { if (value === attrs && String(key).startsWith("accessor")) probes++; return descriptor(value, key); });
  let clean: any;
  try { clean = privacyFilter().span(raw); } finally { spy.mockRestore(); }
  expect(probes).toBe(4096);
  expect(execute).not.toHaveBeenCalled();
  expect(clean.attributes).toEqual({});
  expect(clean.events).toEqual([]);
  expect(clean.links).toEqual([]);
});

test("B-003 bound provider responses are scrubbed at export with discovery charged to the shared ledger", () => {
  const raw = fixture({ "output.value": JSON.stringify({ choices: [{ message: { content: "ordinary answer" }, logprobs: { content: [{ token: "safe token" }] } }], api_key: "r4-provider-synthetic-secret" }), note: "short prose" });
  runtime.rememberProviderResponse(raw._spanContext, raw.attributes, "openai.chat");
  const providerDiscovery = jest.spyOn(runtime, "discoverProviderResponseSecrets");
  let clean: any;
  try { clean = privacyFilter().span(raw); expect(providerDiscovery).toHaveBeenCalledTimes(1); } finally { providerDiscovery.mockRestore(); }
  expect(String(clean.attributes["output.value"])).toContain("ordinary answer");
  expect(String(clean.attributes["output.value"])).not.toContain("r4-provider-synthetic-secret");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
  expect(getRegisteredSecrets()).toContain("r4-provider-synthetic-secret");
});

test("B-001 aggregate discovery failure with an already-lossy lineage is empty and sticky", () => {
  const raw = fixture(); raw.droppedAttributesCount = 1;
  raw.resource.attributes.note = "ordinary prose! ".repeat(300000);
  const clean = privacyFilter().span(raw);
  expect(clean.attributes).toEqual({ "traceai.capture.host_data_unknown": true });
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.events).toEqual([]);
  const next = { ...fixture(), spanContext: clean.spanContext };
  delete next._spanContext;
  const later = privacyFilter().span(next);
  expect(later.attributes).toEqual({ "traceai.capture.host_data_unknown": true });
  expect(later.name).toBe("<unknown: host-truncated>");
  expect(later.status).toEqual({ code: 2 });
  expect(later.events).toEqual([]);
  expect(later.links).toEqual([]);
  expect(later.resource.attributes).toEqual({});
  expect(later.instrumentationScope).toEqual({ name: "<unknown: host-truncated>" });
});
