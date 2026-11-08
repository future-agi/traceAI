import type { Attributes } from "@opentelemetry/api";
import { privacyFilter } from "../export-privacy";
import * as runtime from "../privacy-runtime";
import { getRegisteredSecrets } from "../secrets";

// I1 r10c literal controls. Synthetic values only. Runtime NOT_RUN by this
// writer; RED/GREEN derivations are recorded in 02-tests.json.
const savedEnv = { ...process.env }, registryKey = Symbol.for("traceai.secrets");
let savedRegistry: PropertyDescriptor | undefined;
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  savedRegistry = Object.getOwnPropertyDescriptor(globalThis, registryKey);
  Reflect.deleteProperty(globalThis, registryKey);
});
afterEach(() => {
  jest.restoreAllMocks(); process.env = { ...savedEnv };
  Reflect.deleteProperty(globalThis, registryKey);
  if (savedRegistry) Object.defineProperty(globalThis, registryKey, savedRegistry);
});
const UNKNOWN = "<unknown: host-truncated>", HOST = "traceai.capture.host_data_unknown";
const IDENTITY = { traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 };
function fixture(attributes: Attributes = { note: "r10c span prose" }): any {
  const ctx = { ...IDENTITY };
  return { _spanContext: ctx, spanContext: () => ctx, name: "r10c span name", attributes, kind: 2,
    startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    status: { code: 2, message: "r10c status prose" }, resource: { attributes: { note: "r10c resource prose" } },
    instrumentationScope: { name: "r10c scope", version: "r10c version", schemaUrl: "https://example.invalid/r10c", attributes: { note: "r10c scope prose" } },
    events: [{ name: "r10c event", time: [1, 0], attributes: { note: "r10c event prose" } }],
    links: [{ context: ctx, attributes: { note: "r10c link prose" } }] };
}
// FI refusal after independent host loss: structure, status code and the host flag only.
function expectMinimal(clean: any): void {
  expect(clean.attributes).toEqual({ [HOST]: true });
  expect(clean.name).toBe(UNKNOWN);
  expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]);
  expect(clean.links).toEqual([]);
  expect(clean.resource.attributes).toEqual({});
  expect(clean.instrumentationScope).toEqual({ name: UNKNOWN });
  expect(clean.spanContext()).toEqual(IDENTITY);
}

// (1) Discovery stays on for blanket-withheld content on every surface.
test.each(["resource", "scope", "event", "link", "status", "name", "extra"])(
  "R10C withheld %s credential is discovered, registered and never exported", surface => {
    const secret = `r10c-${surface}-synthetic-teal-quince`, json = JSON.stringify({ api_key: secret });
    const raw = fixture(); raw.droppedAttributesCount = 1;
    const extra: Attributes = {};
    if (surface === "resource") raw.resource.attributes.api_key = secret;
    if (surface === "scope") raw.instrumentationScope.attributes.api_key = secret;
    if (surface === "event") raw.events[0].attributes.api_key = secret;
    if (surface === "link") raw.links[0].attributes.api_key = secret;
    if (surface === "status") raw.status.message = json;
    if (surface === "name") raw.name = json;
    if (surface === "extra") extra.api_key = secret;
    expect(getRegisteredSecrets()).not.toContain(secret);
    const before = JSON.stringify([raw, extra]), clean = privacyFilter().span(raw, [], extra);
    expect(getRegisteredSecrets()).toContain(secret);
    const planted = (target: string): Attributes => surface === target ? { api_key: UNKNOWN } : {};
    expect(clean.attributes).toEqual({ note: UNKNOWN, [HOST]: true });
    expect(clean.name).toBe(UNKNOWN);
    expect(clean.status).toEqual({ code: 2, message: UNKNOWN });
    expect(clean.resource.attributes).toEqual({ note: UNKNOWN, ...planted("resource"), ...planted("extra") });
    expect(clean.instrumentationScope).toEqual({ name: UNKNOWN, version: UNKNOWN, schemaUrl: UNKNOWN, attributes: { note: UNKNOWN, ...planted("scope") } });
    expect(clean.events).toEqual([{ name: UNKNOWN, time: [1, 0], attributes: { note: UNKNOWN, ...planted("event") }, droppedAttributesCount: 0 }]);
    expect(clean.links).toHaveLength(1);
    expect(clean.links[0].attributes).toEqual({ note: UNKNOWN, ...planted("link") });
    expect(clean.droppedAttributesCount).toBe(1);
    expect(JSON.stringify(clean)).not.toContain(secret);
    expect(JSON.stringify([raw, extra])).toBe(before);
    // The registration is effective: a later no-loss span echoing it is scrubbed.
    const echo = privacyFilter().span(fixture({ echo: secret, note: "r10c echo prose" }));
    expect(echo.attributes).toEqual({ echo: "<redacted>", note: "r10c echo prose" });
  });

// (2) Marker probes and registered-secret matching stay off for withheld values.
test("R10C 512 withheld 4096-char chunks with 1024 registered secrets: zero includes probes, discovery still on", () => {
  const chunk = "q".repeat(4096), secret = "r10c-chunk-synthetic-teal-quince", nested = "r10c-json-synthetic-teal-quince";
  const raw = fixture({ ...Object.fromEntries(Array.from({ length: 512 }, (_, i) => [`note${i}`, chunk])), note512: JSON.stringify({ password: nested }), api_key: secret });
  raw._attributeValueLengthLimit = 8;
  raw.status.message = chunk; raw.resource.attributes.note = chunk; raw.instrumentationScope.attributes.note = chunk;
  raw.events[0].attributes = { "exception.message": chunk, "exception.stacktrace": chunk };
  raw.links[0].attributes.note = chunk;
  const filter = privacyFilter({}, Array.from({ length: 1024 }, (_, i) => `s${i.toString(16).padStart(7, "0")}-synthetic-r10c-secret`));
  const before = JSON.stringify(raw), includes = String.prototype.includes, test = RegExp.prototype.test;
  let probes = 0, regexProbes = 0;
  const stringSpy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) {
    if (String(this) === chunk) probes++;
    return includes.call(this, search, position);
  });
  const regexSpy = jest.spyOn(RegExp.prototype, "test").mockImplementation(function (this: RegExp, value: string) {
    if (value === chunk) regexProbes++;
    return test.call(this, value);
  });
  let clean: any;
  try { clean = filter.span(raw); } finally { stringSpy.mockRestore(); regexSpy.mockRestore(); }
  expect(probes).toBe(0);
  expect(regexProbes).toBe(0);
  expect(getRegisteredSecrets()).toContain(secret);
  expect(getRegisteredSecrets()).toContain(nested);
  expect(Object.keys(clean.attributes)).toHaveLength(515);
  expect(clean.attributes.note0).toBe(UNKNOWN); expect(clean.attributes.note511).toBe(UNKNOWN);
  expect(clean.attributes.note512).toBe(UNKNOWN); expect(clean.attributes.api_key).toBe(UNKNOWN);
  expect(clean.attributes[HOST]).toBe(true);
  expect(clean.name).toBe(UNKNOWN);
  expect(clean.status).toEqual({ code: 2, message: UNKNOWN });
  expect(clean.resource.attributes).toEqual({ note: UNKNOWN });
  expect(clean.instrumentationScope).toEqual({ name: UNKNOWN, version: UNKNOWN, schemaUrl: UNKNOWN, attributes: { note: UNKNOWN } });
  expect(clean.events[0].attributes).toEqual({ "exception.message": UNKNOWN, "exception.stacktrace": UNKNOWN });
  expect(clean.links[0].attributes).toEqual({ note: UNKNOWN });
  const exported = JSON.stringify(clean);
  expect(exported).not.toContain(secret); expect(exported).not.toContain(nested); expect(exported).not.toContain("qqqqqqqq");
  expect(JSON.stringify(raw)).toBe(before);
});

// (3a) One debit per withheld source: 3 x 1,100,000 chars fit the 4 MiB allowance.
// Any second debit of the same characters would exceed it and refuse.
test("R10C three 1.1M-char withheld values fit one allowance and a later link credential is discovered", () => {
  const prose = "ordinary! ".repeat(110000), secret = "r10c-later-link-synthetic-teal-quince";
  const raw = fixture({ note: prose }); raw.droppedAttributesCount = 1;
  raw.resource.attributes.note = prose; raw.events[0].attributes.note = prose;
  raw.links[0].attributes.api_key = secret;
  expect(getRegisteredSecrets()).not.toContain(secret);
  const clean = privacyFilter().span(raw);
  expect(getRegisteredSecrets()).toContain(secret);
  expect(clean.attributes).toEqual({ note: UNKNOWN, [HOST]: true });
  expect(clean.name).toBe(UNKNOWN);
  expect(clean.status).toEqual({ code: 2, message: UNKNOWN });
  expect(clean.resource.attributes).toEqual({ note: UNKNOWN });
  expect(clean.events[0].attributes).toEqual({ note: UNKNOWN });
  expect(clean.links[0].attributes).toEqual({ note: UNKNOWN, api_key: UNKNOWN });
  expect(raw.attributes.note).toBe(prose);
}, 30000);

// (3b) Exhaustion keeps the existing sticky FI-uncertainty outcome.
test("R10C four 1.1M-char withheld values exhaust the allowance: minimal, sticky, later credential unread", () => {
  const prose = "ordinary! ".repeat(110000), secret = "r10c-unread-link-synthetic-teal-quince";
  const raw = fixture({ note: prose }); raw.droppedAttributesCount = 1;
  raw.resource.attributes.note = prose; raw.instrumentationScope.attributes.note = prose; raw.events[0].attributes.note = prose;
  raw.links[0].attributes.api_key = secret;
  const clean = privacyFilter().span(raw);
  expectMinimal(clean);
  expect(getRegisteredSecrets()).not.toContain(secret);
  const later = { ...fixture({ note: "r10c short copy" }), spanContext: clean.spanContext };
  delete later._spanContext;
  expectMinimal(privacyFilter().span(later));
  expect(getRegisteredSecrets()).not.toContain(secret);
  expect(raw.attributes.note).toBe(prose);
}, 30000);

// (4) Tracestate is a surface too: discovered under known loss, never exported.
test("R10C tracestate under known host loss is discovered and never exported", () => {
  const secret = "r10c-tracestate-synthetic-teal-quince";
  const raw = fixture(); raw.droppedAttributesCount = 1;
  raw._spanContext.traceState = { _internalState: new Map([["api_key", secret]]) };
  expect(getRegisteredSecrets()).not.toContain(secret);
  const clean = privacyFilter().span(raw);
  expect(getRegisteredSecrets()).toContain(secret);
  expect(clean.attributes).toEqual({ note: UNKNOWN, [HOST]: true });
  expect(clean.spanContext()).toEqual(IDENTITY);
  expect(clean.spanContext().traceState).toBeUndefined();
  expect(clean.links[0].context).toEqual(IDENTITY);
  expect(JSON.stringify(clean)).not.toContain(secret);
});

// (5) A withheld bound provider response keeps format-aware discovery.
test("R10C withheld bound provider response is discovered once through the provider path", () => {
  const secret = "r10c-provider-synthetic-teal-quince";
  const raw = fixture({ "output.value": JSON.stringify({ choices: [{ message: { content: "r10c answer" }, logprobs: { content: [{ token: "r10c token" }] } }], api_key: secret }), note: "r10c span prose" });
  raw.droppedAttributesCount = 1;
  runtime.rememberProviderResponse(raw._spanContext, raw.attributes, "openai.chat");
  const providerDiscovery = jest.spyOn(runtime, "discoverProviderResponseSecrets");
  let clean: any;
  try { clean = privacyFilter().span(raw); expect(providerDiscovery).toHaveBeenCalledTimes(1); } finally { providerDiscovery.mockRestore(); }
  expect(getRegisteredSecrets()).toContain(secret);
  expect(clean.attributes).toEqual({ "output.value": UNKNOWN, note: UNKNOWN, [HOST]: true });
  expect(JSON.stringify(clean)).not.toContain(secret);
});
