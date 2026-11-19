import { ROOT_CONTEXT, SpanKind, trace, propagation, baggageEntryMetadataFromString } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { registerSecret } from "@traceai/fi-core";
import { compileOutgoingPolicy, allowsOutgoingDestination, normalizePropagationHost } from "../src/outgoing-policy";
import { createOutgoingPrivacy, noteOutgoingSpanStart, type OutgoingPrivacy } from "../src/outgoing-privacy";
import { stageNativeOutgoingHeaders as stageNativeWithAuthority, installOutgoingPropagation } from "../src/outgoing-node";

// These carrier-only fixtures model a known HTTP transport, not a Host-derived
// connection. Real adapters must obtain authority from their connection seam.
const stageNativeOutgoingHeaders = (...args: Parameters<typeof stageNativeWithAuthority>) =>
  stageNativeWithAuthority(args[0], args[1], args[2], args[3], args[4], args[5], args[6] ?? new URL("http://orders.internal"));
import { resolveConfig, maskConfig, outgoingPolicyForConfig } from "../src/config";
import { providerForDestination, loadCatalog, match } from "../src/wire/core";
import { observePropagationHead } from "../src/wire/observe-node";
import { privacyFilter } from "../src/export-privacy";
import type { IntegrationStatus } from "../src/libraries";

// Candidate-only helper/carrier regression population. Missing helper imports at
// the base are NOT transport RED evidence. Real receipt lives in the other file.
const policy = compileOutgoingPolicy(undefined, undefined);
const destination = new URL("http://orders.internal:8080/ordinary");
const remote = trace.setSpanContext(ROOT_CONTEXT, { traceId: "13579bdf13579bdf13579bdf13579bdf", spanId: "2468ace02468ace0", traceFlags: 0, isRemote: true });
const parent = "00-13579bdf13579bdf13579bdf13579bdf-2468ace02468ace0-00";
const privacy = createOutgoingPrivacy();
const prepare = (ctx = remote, pairs: readonly (readonly [string, string])[] = []) => privacy.prepareOutgoingHeaders(ctx, destination, pairs, policy);

test.each([
  ["Orders.Internal.", "orders.internal"], ["bücher.example", "xn--bcher-kva.example"], ["127.0.0.1", "127.0.0.1"], ["[::1]", "[::1]"],
  ["127.1", undefined], ["0x7f000001", undefined], ["https://orders.internal", undefined], ["orders.internal:80", undefined], ["orders.internal..", undefined], ["evil\\host", undefined],
])("R10 canonical host %s", (raw, expected) => { expect(normalizePropagationHost(raw!)).toBe(expected); });
test.each(["*", "bad/path", "orders.internal:80", "-", "api.openai.com,", "+*.127.0.0.1", "a@b", "-https://vendor.invalid", "-*.example..", "-foo%2eexample"])("R10 invalid whole policy fails closed: %s", input => {
  expect(compileOutgoingPolicy("all", input)).toMatchObject({ mode: "none", valid: false });
});
test("R09 bounded policy and deny/allowlist precedence", () => {
  expect(compileOutgoingPolicy("invalid", undefined).valid).toBe(false);
  expect(compileOutgoingPolicy(false, undefined).valid).toBe(false);
  expect(compileOutgoingPolicy("all", Array(257).fill("a.example")).valid).toBe(false);
  expect(compileOutgoingPolicy("all", "a".repeat(17000)).valid).toBe(false);
  expect(compileOutgoingPolicy(" ", undefined).mode).toBe("default");
  const allowed = (rules: string, host: string, mode = "default") => allowsOutgoingDestination(compileOutgoingPolicy(mode, rules), new URL(`https://${host}/health`));
  expect(allowed("+*.svc.example,-payments.svc.example", "orders.svc.example")).toBe(true);
  expect(allowed("+*.svc.example,-payments.svc.example", "payments.svc.example")).toBe(false);
  expect(allowed("+*.svc.example", "svc.example")).toBe(false);
  expect(allowed("+*.svc.example", "badsvc.example")).toBe(false);
  expect(allowed("+orders.internal", "other.internal", "all")).toBe(false);
  // R6: positive rules narrow admission; they cannot waive the catalog veto.
  expect(allowed("+api.openai.com", "api.openai.com")).toBe(false);
  for (const rules of ["+api.openai.com,-api.openai.com", "-api.openai.com,+api.openai.com"]) expect(allowed(rules, "api.openai.com", "all")).toBe(false);
  expect(allowed("+orders.internal", "orders.internal", "none")).toBe(false);
  expect(allowsOutgoingDestination(policy, new URL("http://127.0.0.1:11434/health"))).toBe(false);
});
test("R08 G08 every catalog provider shape is recognized without endpoint matching", () => {
  for (const provider of loadCatalog().providers) {
    const samples = [...(provider.hosts ?? []), ...(provider.host_suffixes ?? []).map(suffix => provider.match === "all" ? `${provider.host_prefixes![0]}us-test${suffix}` : `tenant${suffix}`), ...(provider.ports ?? []).map(() => "loopback.internal")];
    expect(samples.length).toBeGreaterThan(0);
    for (const host of samples) {
      const port = provider.ports?.[0] ?? 443;
      expect(providerForDestination(host, port)).toBe(provider.name);
      expect(allowsOutgoingDestination(policy, new URL(`https://${host}:${port}/unknown-path`))).toBe(false);
    }
  }
  expect(providerForDestination("bedrock-runtime.orders.internal", 443)).toBeUndefined();
  expect(providerForDestination("ordinary.amazonaws.com", 443)).toBeUndefined();
  expect(providerForDestination("api.openai.com.evil.invalid", 443)).toBeUndefined();
  expect(providerForDestination("api.openai.com", 11434)).toBe("openai");
  expect(match("POST", "http://orders.internal/v1/chat/completions")).toMatchObject({ provider: "openai_compatible", knownProvider: false });
  expect(match("POST", "https://api.openai.com/v1/chat/completions")).toMatchObject({ provider: "openai", knownProvider: true });
});
test("R10 code/env/file precedence and host-list privacy", () => {
  const dir = mkdtempSync(join(tmpdir(), "f4-policy-"));
  mkdirSync(join(dir, ".fi")); writeFileSync(join(dir, ".fi/config.toml"), "");
  const cwd = jest.spyOn(process, "cwd").mockReturnValue(dir), warn = jest.spyOn(console, "warn").mockImplementation(() => {});
  const file = join(dir, "futureagi.toml");
  writeFileSync(file, 'propagate_outgoing = "all"\npropagate_hosts = ["+file-private.internal"]\n');
  try {
    const env = { HOME: dir, XDG_CONFIG_HOME: dir, TRACEAI_PROPAGATE_OUTGOING: "none", TRACEAI_PROPAGATE_HOSTS: "+env-private.internal" };
    const code = resolveConfig({ configFile: file, propagateOutgoing: "default", propagateHosts: ["+code-private.internal", "-denied.internal"] }, env);
    expect(maskConfig(code).outgoingPropagation).toEqual({ mode: "default", valid: true, allowRules: 1, denyRules: 1 });
    expect(outgoingPolicyForConfig(code).rules[0].host).toBe("code-private.internal");
    expect(JSON.stringify(code) + JSON.stringify(maskConfig(code))).not.toMatch(/code-private|file-private|env-private|denied.internal/);
    expect(resolveConfig({ configFile: file }, env).propagateOutgoing).toBe("none");
    expect(resolveConfig({ configFile: file }, { HOME: dir, XDG_CONFIG_HOME: dir }).propagateOutgoing).toBe("all");
    const invalid = resolveConfig({ configFile: file, propagateHosts: ["-synthetic-private/bad"] }, env);
    expect(maskConfig(invalid).outgoingPropagation).toMatchObject({ mode: "none", valid: false });
    expect(JSON.stringify(warn.mock.calls)).not.toContain("synthetic-private");
    expect(maskConfig(resolveConfig({ configFile: file, propagateHosts: "+bad-code-type.internal" as any }, env)).outgoingPropagation).toMatchObject({ mode: "none", valid: false });
    for (const raw of ['["-vendor.internal", false]', '{"deny":"vendor.internal"}', '"-vendor.internal"', '[broken', '']) {
      writeFileSync(file, `propagate_outgoing = "all"\npropagate_hosts = ${raw}\n`);
      expect(maskConfig(resolveConfig({ configFile: file }, { HOME: dir, XDG_CONFIG_HOME: dir })).outgoingPropagation).toMatchObject({ mode: "none", valid: false });
    }
  } finally { cwd.mockRestore(); warn.mockRestore(); rmSync(dir, { recursive: true, force: true }); }
});
test("R06 R12 real FI positive controls do not create IDs or baggage", () => {
  expect(prepare()?.get("traceparent")).toBe(parent);
  expect(prepare()?.get("baggage")).toBeUndefined();
  expect(prepare(ROOT_CONTEXT)).toBeUndefined();
  const baggage = propagation.createBaggage({ region: { value: "west" } });
  const ctx = propagation.setBaggage(ROOT_CONTEXT, baggage);
  expect([...prepare(ctx)!]).toEqual([["baggage", "region=west"]]);
  expect(baggage.getAllEntries()).toEqual([["region", { value: "west" }]]);
});
test.each(["traceparent", "TraceParent", "TRACEPARENT"])("G01 presence of %s vetoes the whole batch", key => {
  for (const value of ["", "invalid", parent]) expect(prepare(remote, [[key, value], [key, value]])).toBeUndefined();
});
test("G01 application baggage and tracestate are not overwritten", () => {
  const ctx = propagation.setBaggage(remote, propagation.createBaggage({ region: { value: "west" } }));
  const result = prepare(ctx, [["Baggage", "app=owned"], ["Tracestate", "app=owned"]]);
  expect([...result!]).toEqual([["traceparent", parent]]);
});
test("G02 CLIENT, opaque local and suppressed contexts veto additions", async () => {
  const provider = new BasicTracerProvider({ spanProcessors: [{ onStart: noteOutgoingSpanStart, onEnd() {}, async forceFlush() {}, async shutdown() {} }] });
  try {
    for (const kind of [SpanKind.INTERNAL, SpanKind.SERVER, SpanKind.PRODUCER, SpanKind.CONSUMER, SpanKind.CLIENT]) {
      const span = provider.getTracer("f4").startSpan("synthetic", { kind });
      const result = prepare(trace.setSpan(ROOT_CONTEXT, span));
      expect(!!result).toBe(kind !== SpanKind.CLIENT); span.end();
    }
    expect(prepare(suppressTracing(remote))).toBeUndefined();
    expect(prepare(trace.setSpanContext(ROOT_CONTEXT, { ...trace.getSpanContext(remote)!, isRemote: false }))).toBeUndefined();
    expect(prepare(trace.setSpanContext(ROOT_CONTEXT, { traceId: "0".repeat(32), spanId: "0".repeat(16), traceFlags: 1, isRemote: true }))).toBeUndefined();
  } finally { await provider.shutdown(); }
});
test("R11 structured baggage/metadata are FI-vetted, not redacted onto the wire", () => {
  const baggage = propagation.createBaggage({
    region: { value: "west" }, authorization: { value: "synthetic-private-auth-value" },
    link: { value: "https://example.invalid/?token=synthetic-signed-value" },
    nested: { value: '{"password":"synthetic-nested-value"}' },
    metadata: { value: "safe", metadata: baggageEntryMetadataFromString("token=synthetic-private-meta") },
  });
  const before = baggage.getAllEntries();
  expect(prepare(propagation.setBaggage(remote, baggage))?.get("baggage")).toBe("region=west");
  expect(baggage.getAllEntries()).toEqual(before);
  const overflow = propagation.createBaggage(Object.fromEntries(Array.from({ length: 65 }, (_, i) => [`key${i}`, { value: "west" }])));
  expect(prepare(propagation.setBaggage(remote, overflow))?.get("baggage")).toBeUndefined();
  expect(prepare(propagation.setBaggage(remote, propagation.createBaggage({ huge: { value: "x".repeat(4097) } })))?.get("baggage")).toBeUndefined();
});
test("G05 duplicate credential observation uses the existing FI registry", () => {
  expect(observePropagationHead(destination.href, [["authorization", "Bearer synthetic-first-private"], ["authorization", "Bearer synthetic-second-private"]])).toBe(true);
  const filtered = privacyFilter().attributes({ first: "synthetic-first-private", second: "synthetic-second-private" });
  expect(JSON.stringify(filtered)).not.toMatch(/synthetic-first-private|synthetic-second-private/);
});
const req = { host: "orders.internal", protocol: "http:", method: "POST" };
const line = "POST /ordinary HTTP/1.1\r\n";
test.each(["map", "pairs", "flat"])("G06 native %s carrier staging is private and preserves duplicates", form => {
  const headers: any = form === "map" ? { host: ["Host", "orders.internal:8080"], "x-duplicate": ["X-Duplicate", ["one", "two"]], "content-length": ["Content-Length", 6] } : form === "pairs" ? [["Host", "orders.internal:8080"], ["X-Duplicate", "one"], ["X-Duplicate", "two"]] : ["Host", "orders.internal:8080", "X-Duplicate", "one", "X-Duplicate", "two"];
  const before = JSON.stringify(headers);
  const stage = stageNativeOutgoingHeaders(req, line, headers, remote, policy, privacy, new URL("http://orders.internal:8080"));
  expect(stage?.additions.get("traceparent")).toBe(parent);
  expect(JSON.stringify(headers)).toBe(before); expect(stage!.headers).not.toBe(headers);
  expect(JSON.stringify(stage!.headers)).toContain("one"); expect(JSON.stringify(stage!.headers)).toContain("two");
});
test("G02 G06 unknown/accessor/frozen carriers and ambiguous authorities skip", () => {
  const getter = jest.fn(() => "must-not-read");
  const headers = Object.defineProperty({}, "host", { get: getter });
  expect(stageNativeOutgoingHeaders(req, line, headers, remote, policy, privacy)).toBeUndefined();
  expect(getter).not.toHaveBeenCalled();
  expect(stageNativeOutgoingHeaders(req, line, Object.freeze({ host: ["Host", "orders.internal"] }), remote, policy, privacy)).toBeUndefined();
  for (const first of ["CONNECT orders.internal:443 HTTP/1.1\r\n", "POST http://api.openai.com/ordinary HTTP/1.1\r\n", "POST //api.openai.com/ordinary HTTP/1.1\r\n"]) expect(stageNativeOutgoingHeaders(req, first, { host: ["Host", "orders.internal"] }, remote, policy, privacy)).toBeUndefined();
});
test("R15 staging exceptions add nothing and leave original carriers untouched", () => {
  const throwing: OutgoingPrivacy = { prepareOutgoingHeaders() { throw new Error("synthetic private failure must not escape"); } };
  const native = { host: ["Host", "orders.internal"] };
  expect(stageNativeOutgoingHeaders(req, line, native, remote, policy, throwing)).toBeUndefined();
  expect(native).toEqual({ host: ["Host", "orders.internal"] });
  const badContext = { getValue() { throw new Error("synthetic context failure"); } } as any;
  expect(prepare(badContext)).toBeUndefined();
});
test("qualification is explicitly closed, not falsely reported as pending/instrumented", () => {
  const statuses: IntegrationStatus[] = [], warn = jest.fn();
  const remove = installOutgoingPropagation(policy, privacy, statuses, warn);
  expect(statuses.map(s => [s.name, s.status])).toEqual([["outgoing-http", "disabled"]]);
  expect(statuses.every(s => s.reason === "Outgoing runtime qualification has not been completed")).toBe(true);
  remove(); remove(); expect(warn).not.toHaveBeenCalled();
});
test("G03 a registered trace identity is not exempted from FI masking", () => {
  const id = "abcdef12abcdef12abcdef12abcdef12";
  registerSecret(id);
  const ctx = trace.setSpanContext(ROOT_CONTEXT, { traceId: id, spanId: "abcdef12abcdef12", traceFlags: 1, isRemote: true });
  expect(prepare(ctx)).toBeUndefined();
});
