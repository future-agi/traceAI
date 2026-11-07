import { registerSecret, getRegisteredSecrets, seedSecrets, markSecretDiscoveryFailed } from "@traceai/fi-core";
import { channel } from "node:diagnostics_channel";
import { observeOutgoing, observeTransports } from "../src/wire/observe-node";
import { retainBodySecrets, RequestData } from "../src/wire/core";
import { discoverSecrets, CREDENTIAL_HEADERS, credentialSecrets } from "../src/wire/privacy-runtime";
import { privacyFilter } from "../src/export-privacy";
const KEY = Symbol.for("traceai.secrets");
let saved: PropertyDescriptor | undefined;
beforeEach(() => { saved = Object.getOwnPropertyDescriptor(globalThis, KEY); delete (globalThis as any)[KEY]; });
afterEach(() => { delete (globalThis as any)[KEY]; if (saved) Object.defineProperty(globalThis, KEY, saved); });

test.each(CREDENTIAL_HEADERS)("S5 shared credential list recognizes transport and export field %s", name => {
  const secret = `private-header-credential-${name}`, value = /authorization$/.test(name) ? `Bearer ${secret}` : secret;
  expect(credentialSecrets({ [name.toUpperCase()]: value })).toContain(secret);
  expect(discoverSecrets([{ [`http.request.header.${name.toUpperCase()}`]: [value] }])).toContain(secret);
  const source = fake({ [`http.request.header.${name.toUpperCase()}`]: [value], echoed: secret });
  source.status = { code: 2, message: `Invalid ${secret}` }; source.events = [{ name: secret, attributes: { echoed: secret } }];
  expect(contents(privacyFilter().span(source))).not.toContain(secret);
  expect(source.attributes.echoed).toBe(secret);
});
test("S5 registry ignores short/unsupported values without coercion and is longest first", () => {
  const execute = jest.fn(() => { throw new Error("must not run"); });
  for (const value of ["short", 12345678, null, { toString: execute }, undefined]) registerSecret(value);
  registerSecret("private-token"); registerSecret("longer-private-token");
  expect(getRegisteredSecrets()).toEqual(["longer-private-token", "private-token"]); expect(execute).not.toHaveBeenCalled();
  expect(privacyFilter().attributes({ echoed: "longer-private-token private-token" }).echoed).toBe("<redacted> <redacted>");
});
test("S5 registry bounds 1024 values with observation-recency LRU eviction and immutable cached snapshots", () => {
  for (let i = 0; i < 1024; i++) registerSecret(`private-token-${i}`);
  const before = getRegisteredSecrets(); expect(getRegisteredSecrets()).toBe(before); expect(Object.isFrozen(before)).toBe(true);
  registerSecret("private-token-0"); registerSecret("private-token-1024");
  const after = getRegisteredSecrets(); expect(after).toHaveLength(1024); expect(after).toContain("private-token-0");
  expect(after).not.toContain("private-token-1"); expect(after).toContain("private-token-1024"); expect(before).not.toContain("private-token-1024");
  // Exceeding the old per-context limit must not drop ordinary telemetry.
  expect(privacyFilter().span(fake({ safe: "still visible" })).attributes.safe).toBe("still visible");
});
test("S5 configured FI credentials remain protected after registry LRU churn", () => {
  const secret = "configured-fi-key-before-churn", filter = privacyFilter({}, [secret]);
  for (let i = 0; i < 1024; i++) registerSecret(`churn-credential-value-${i}`);
  expect(getRegisteredSecrets()).not.toContain(secret);
  const source = fake({ echoed: secret }); source.status = { code: 2, message: secret };
  expect(contents(filter.span(source))).not.toContain(secret); expect(getRegisteredSecrets()).toHaveLength(1024);
});
test("S5 initialization seeds configured FI keys and every credential-like environment name", () => {
  seedSecrets({ API_KEY: "environment-api-key", MyToKeN: "environment-token", SECRET: "environment-secret", PASSWORD: "environment-password", PASSWD: "environment-passwd", CREDENTIAL_FILE: "environment-credential", AUTH: "environment-auth", KEY_SHORT: "short", ORDINARY: "ordinary-value" }, ["configured-fi-api-key", "configured-fi-secret"]);
  expect(getRegisteredSecrets()).toHaveLength(9); expect(getRegisteredSecrets()).not.toContain("short"); expect(getRegisteredSecrets()).not.toContain("ordinary-value");
});
test("S5 a span started before registration and unrelated traces scrub every export surface without mutating sources", () => {
  const secret = "late-registered-private-token", span = fake({ array: [secret], echoed: secret });
  span.status = { code: 2, message: secret }; span.events = [{ name: secret, attributes: { echoed: secret } }];
  span.resource.attributes = { echoed: secret }; span.instrumentationScope = { name: secret, version: secret, attributes: { echoed: secret } };
  const { createTraceState } = require("@opentelemetry/api"); span._spanContext.traceState = createTraceState(`vendor=${secret}`);
  span.parentSpanContext = span._spanContext; span.links = [{ context: span._spanContext, attributes: { echoed: secret } }];
  const filter = privacyFilter(); registerSecret(secret);
  expect(contents(filter.span(span))).not.toContain(secret); expect(contents(filter.span(fake({ unrelated: secret })))).not.toContain(secret);
  expect(contents(span)).toContain(secret);
});
test("S5 registry access failure drops content, keeps identity and timing, and never logs values", () => {
  Object.defineProperty(globalThis, KEY, { configurable: true, value: { register() {}, values() { throw new Error("private registry failure"); } } });
  expect(() => getRegisteredSecrets()).toThrow("private registry failure");
  const log = jest.spyOn(console, "warn").mockImplementation(() => {});
  try {
    const input = fake({ echoed: "private-content" }), output = privacyFilter().span(input);
    expect(output.attributes).toEqual({}); expect(output.startTime).toEqual(input.startTime); expect(output.spanContext().traceId).toBe(input._spanContext.traceId);
    expect(contents(output)).not.toContain("private"); expect(log).not.toHaveBeenCalled();
  } finally { log.mockRestore(); }
});
test("shared discovery retains numeric/array credentials and encoded signed URLs without invoking getters", () => {
  const token = "private+/token=12345", encoded = encodeURIComponent(token), getter = jest.fn();
  const source = Object.defineProperty({ password: [12345678, "array-private-token"], arguments: JSON.stringify({ authorization_token: "nested-private-token" }), link: `https://cdn.test/?%74oken=${encoded}` }, "accessor", { get: getter, enumerable: true });
  expect(discoverSecrets([source])).toEqual(expect.arrayContaining(["12345678", "array-private-token", "nested-private-token", token, encoded]));
  expect(getter).not.toHaveBeenCalled();
});
test("S5 long credentials and more than 64 query values register without the retired per-context budgets", () => {
  const values = Array.from({ length: 80 }, (_, i) => `credential-${i}-` + "L".repeat(8217));
  const url = "http://127.0.0.1/?" + new URLSearchParams(values.map(value => ["token", value]));
  observeOutgoing({ method: "GET", url, headers: {} });
  expect(getRegisteredSecrets()).toEqual(expect.arrayContaining(values));
  expect(privacyFilter().attributes({ echo: values[0] }).echo).toBe("<redacted>");
});
test.each(["outgoing", "diagnostic", "retained-body"])("S5 %s discovery exhaustion makes every later export fail closed without changing application data", mode => {
  const secret = "unknown-private-transport-credential", filter = privacyFilter();
  // Cache a successful snapshot first; a stale snapshot must never bypass failure.
  expect(filter.span(fake({ safe: "ordinary text" })).attributes.safe).toBe("ordinary text");
  const url = "http://127.0.0.1/?" + new URLSearchParams(Array.from({ length: 1025 }, (_, i) => ["token", `${secret}-${i}`]));
  const req: RequestData = { method: "POST", url, headers: {} };
  const log = jest.spyOn(console, "warn").mockImplementation(() => {});
  const remove = mode === "diagnostic" ? observeTransports() : undefined;
  try {
    if (mode === "retained-body") retainBodySecrets(req, JSON.stringify({ padding: "x".repeat(4194304), token: secret }));
    expect(() => mode === "diagnostic" ? channel("undici:request:create").publish({ request: { path: url, headers: "" } }) : observeOutgoing(req)).not.toThrow();
    for (let i = 0; i < 2; i++) {
      const source = fake({ echo: secret }); source.status = { code: 2, message: secret }; source.resource.attributes = { echo: secret };
      source.events = [{ name: secret, attributes: { echo: secret } }]; source.instrumentationScope = { name: secret }; source.links = [{ context: source._spanContext, attributes: { echo: secret } }];
      const clean = filter.span(source);
      expect(contents(clean)).not.toContain(secret); expect(clean.attributes).toEqual({}); expect(clean.status.message).toBeUndefined();
      expect(clean.spanContext().traceId).toBe(source._spanContext.traceId); expect(clean.startTime).toEqual(source.startTime); expect(clean.endTime).toEqual(source.endTime);
      expect(source.attributes.echo).toBe(secret);
    }
    expect(getRegisteredSecrets).toThrow("Privacy credential discovery incomplete"); expect(log).not.toHaveBeenCalled();
  } finally { remove?.(); log.mockRestore(); }
});
test("S5 explicit registry failure is irreversible across registrations and sanitized copies", () => {
  registerSecret("known-private-credential"); const before = getRegisteredSecrets(); markSecretDiscoveryFailed(); registerSecret("later-private-credential");
  expect(before).toContain("known-private-credential"); expect(getRegisteredSecrets).toThrow("Privacy credential discovery incomplete");
  expect(privacyFilter().span(fake({ echo: "later-private-credential" })).attributes).toEqual({});
});
test("S5 failure latches across older register/values-only package copies without creating another secret store", () => {
  const register = jest.fn();
  Object.defineProperty(globalThis, KEY, { configurable: true, value: Object.freeze({ register, values: () => Object.freeze([]) }) });
  const olderCopyValues = () => (globalThis as any)[KEY].values();
  markSecretDiscoveryFailed(); const failedHolder = (globalThis as any)[KEY]; markSecretDiscoveryFailed();
  expect((globalThis as any)[KEY]).toBe(failedHolder); expect(olderCopyValues).toThrow("Privacy credential discovery incomplete");
  registerSecret("legacy-copy-private-credential"); expect(register).toHaveBeenCalledWith("legacy-copy-private-credential");
  expect(privacyFilter().span(fake({ echo: "unknown-private-credential" })).attributes).toEqual({});
});
test("shared discovery rejects oversized JSON instead of silently skipping nested credentials", () => {
  const body = JSON.stringify({ padding: "x".repeat(4194304), authorization_token: "embedded-private-token-12345" });
  expect(() => discoverSecrets([body])).toThrow("Privacy discovery body budget");
});
function fake(attributes: any): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext() { return this._spanContext; }, name: "app", kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], attributes, status: { code: 1 }, events: [], links: [], resource: { attributes: {} }, instrumentationScope: { name: "app" } };
}
function contents(span: any) { return JSON.stringify({ ...span, context: span.spanContext(), resource: span.resource.attributes, tracestate: span.spanContext().traceState?.serialize(), links: span.links.map((l: any) => ({ ...l, tracestate: l.context.traceState?.serialize() })) }); }
