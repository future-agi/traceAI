import { ROOT_CONTEXT, propagation, trace, createTraceState } from "@opentelemetry/api";
import { allowsOutgoingDestination, compileOutgoingPolicy, normalizePropagationHost } from "../src/outgoing-policy";
import { createOutgoingPrivacy } from "../src/outgoing-privacy";
import { stageNativeOutgoingHeaders } from "../src/outgoing-node";
import { loadCatalog } from "../src/wire/core";

// R-LIT: synthetic W3C identity, not a credential (F4-R7-context).
const ctx = propagation.setBaggage(trace.setSpanContext(ROOT_CONTEXT, {
  traceId: "13579bdf13579bdf13579bdf13579bdf", spanId: "2468ace02468ace0", traceFlags: 0, isRemote: true,
  traceState: createTraceState("region=west"),
}), propagation.createBaggage({ region: { value: "west" } }));
const privacy = createOutgoingPrivacy();

test.each([
  ["ORDERS.INTERNAL.", "orders.internal"], ["BÜCHER.internal。", "xn--bcher-kva.internal"],
  ["XN--BCHER-KVA.INTERNAL．", "xn--bcher-kva.internal"], ["Orders｡Internal｡", "orders.internal"],
  ["api。openai。com。", "api.openai.com"], ["[2001:DB8::1]", "[2001:db8::1]"],
  ["orders.internal..", undefined], ["orders.internal。.", undefined], ["orders.internal.。", undefined],
  ["orders.internal。。", undefined], ["0x7f000001", undefined], ["127.1", undefined],
  ["orders.internal/path", undefined], ["orders.internal:11434", undefined],
])("R7 host normalization applies IDNA before stripping exactly one dot: %s", (raw, expected) => {
  expect(normalizePropagationHost(raw!)).toBe(expected);
});

test("R7 allow and deny rules share case, IDNA and terminal-dot normalization", () => {
  const allowed = compileOutgoingPolicy("all", "+BÜCHER.internal。,-blocked。internal。");
  expect(allowed.valid).toBe(true);
  for (const spelling of ["bücher.internal", "XN--BCHER-KVA.INTERNAL.", "bücher。internal。"]) {
    expect(allowsOutgoingDestination(allowed, new URL(`https://${spelling}/health`))).toBe(true);
  }
  const denied = compileOutgoingPolicy("all", "-BÜCHER.internal。");
  expect(denied.valid).toBe(true);
  expect(allowsOutgoingDestination(denied, new URL("https://xn--bcher-kva.internal./health"))).toBe(false);
  const wildcard = compileOutgoingPolicy("all", "+*.SVC。INTERNAL。,-PRIVATE.SVC.INTERNAL.");
  expect(wildcard.valid).toBe(true);
  expect(allowsOutgoingDestination(wildcard, new URL("http://orders.svc.internal./"))).toBe(true);
  expect(allowsOutgoingDestination(wildcard, new URL("http://private.svc.internal/"))).toBe(false);
});

test("R7 every catalog shape remains excluded after case, dot and IDNA mapping", () => {
  for (const provider of loadCatalog().providers) {
    const hosts = [...(provider.hosts ?? []), ...(provider.host_suffixes ?? []).map(suffix => provider.match === "all" ? `${provider.host_prefixes![0]}us-test${suffix}` : `tenant${suffix}`)];
    for (const host of hosts) for (const spelling of [host.toUpperCase() + ".", host.replace(/\./g, "。") + "。"]) {
      for (const mode of ["default", "all"]) for (const protocol of ["http:", "https:"]) {
        const policy = compileOutgoingPolicy(mode, `+${spelling}`);
        expect(policy.valid).toBe(true);
        expect(allowsOutgoingDestination(policy, new URL(`${protocol}//${spelling}/health`))).toBe(false);
      }
    }
  }
});

test("R7 normalized host proof never hides the effective provider port or vendor", () => {
  for (const protocol of ["http:", "https:"]) {
    for (const host of ["ORDERS.INTERNAL.", "BÜCHER.INTERNAL."]) {
      const physical = new URL(`${protocol}//${host}:11434/`);
      const policy = compileOutgoingPolicy("all", `+${host}`);
      expect(allowsOutgoingDestination(policy, physical)).toBe(false);
      const req = { protocol, host: physical.hostname, method: "POST" };
      for (const advertised of [physical.hostname, `${physical.hostname}:80`, `${physical.hostname}:443`, physical.host]) {
        const headers = { host: ["Host", advertised], "x-original": ["X-Original", "preserved"] }, before = JSON.stringify(headers);
        expect(stageNativeOutgoingHeaders(req, "POST /ordinary HTTP/1.1\r\n", headers, ctx, policy, privacy, physical)).toBeUndefined();
        expect(JSON.stringify(headers)).toBe(before);
      }
      const ordinary = new URL(`${protocol}//${host}/`);
      expect(stageNativeOutgoingHeaders(req, "POST /ordinary HTTP/1.1\r\n", { host: ["Host", ordinary.host] }, ctx, policy, privacy, ordinary)?.additions.has("traceparent")).toBe(true);
    }
    const vendor = new URL(`${protocol}//API.OPENAI.COM.:8080/`), policy = compileOutgoingPolicy("all", "+API.OPENAI.COM.");
    const req = { protocol, host: vendor.hostname, method: "POST" }, headers = { host: ["Host", vendor.host] };
    expect(stageNativeOutgoingHeaders(req, "POST /ordinary HTTP/1.1\r\n", headers, ctx, policy, privacy, vendor)).toBeUndefined();
  }
});
