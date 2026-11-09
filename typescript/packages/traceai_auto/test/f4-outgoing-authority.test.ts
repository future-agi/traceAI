import { ROOT_CONTEXT, trace, propagation, createTraceState } from "@opentelemetry/api";
import { compileOutgoingPolicy } from "../src/outgoing-policy";
import { createOutgoingPrivacy } from "../src/outgoing-privacy";
import { stageNativeOutgoingHeaders } from "../src/outgoing-node";

// RED-compatible with the round-2 signature: its implementation ignores the new
// final authority argument. That must fail assertions, not TypeScript compilation.
const native = stageNativeOutgoingHeaders as (...args: any[]) => ReturnType<typeof stageNativeOutgoingHeaders>;
const policy = compileOutgoingPolicy(undefined, undefined), privacy = createOutgoingPrivacy();
// synthetic test value (fi-security F4-R3-context)
const remote = propagation.setBaggage(trace.setSpanContext(ROOT_CONTEXT, {
  traceId: "13579bdf13579bdf13579bdf13579bdf", spanId: "2468ace02468ace0", traceFlags: 0, isRemote: true,
  traceState: createTraceState("region=west"),
}), propagation.createBaggage({ region: { value: "west" } }));
const parent = "00-13579bdf13579bdf13579bdf13579bdf-2468ace02468ace0-00";
const policies = [policy, compileOutgoingPolicy("all", "-api.openai.com,+orders.internal")];

test("R3 native path resolution cannot replace the verified authority", () => {
  for (const origin of ["http://api.openai.com", "http://api.openai.com:8080"]) {
    const authority = new URL(origin), req = { host: authority.hostname, protocol: authority.protocol, method: "POST" };
    const headers = { host: ["Host", authority.host], "x-original": ["X-Original", "preserved"] };
    const before = JSON.stringify(headers);
    for (const path of ["/\\orders.internal/ordinary", "//orders.internal/ordinary", "http://orders.internal/ordinary", "https://orders.internal/ordinary"]) {
      for (const selected of policies) {
        expect(native(req, `POST ${path} HTTP/1.1\r\n`, headers, remote, selected, privacy, authority)).toBeUndefined();
        expect(JSON.stringify(headers)).toBe(before);
      }
    }
  }
  const authority = new URL("http://orders.internal:8080"), req = { host: authority.hostname, protocol: authority.protocol, method: "POST" };
  const headers = { host: ["Host", authority.host] };
  for (const path of ["/ordinary", "http://orders.internal:8080/ordinary"]) {
    expect(native(req, `POST ${path} HTTP/1.1\r\n`, headers, remote, policy, privacy, authority)?.additions.get("traceparent")).toBe(parent);
  }
  for (const path of ["/\\orders.internal:8080/ordinary", "//orders.internal:8080/ordinary", "http://orders.internal/ordinary", "https://orders.internal:8080/ordinary", "/\tordinary", "/ordinary#fragment"]) {
    expect(native(req, `POST ${path} HTTP/1.1\r\n`, headers, remote, policy, privacy, authority)).toBeUndefined();
  }
});

test("R3 native effective ports cannot be reconstructed from Host", () => {
  for (const scheme of ["http:", "https:"]) for (const hostname of ["127.0.0.1", "orders.internal", "[::1]"]) {
    const physical = new URL(`${scheme}//${hostname}:11434/`);
    const req = { host: hostname, protocol: scheme, method: "POST" };
    for (const host of [hostname, `${hostname}:80`, `${hostname}:443`, `${hostname}:11434`]) {
      const headers = { host: ["Host", host], baggage: ["Baggage", "app=owned"], "x-original": ["X-Original", "preserved"] };
      const before = JSON.stringify(headers);
      expect(native(req, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, privacy, physical)).toBeUndefined();
      expect(JSON.stringify(headers)).toBe(before);
    }
    const ordinary = new URL(`${scheme}//${hostname}/`);
    const headers = { host: ["Host", `${hostname}:11434`] };
    expect(native(req, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, privacy, ordinary)).toBeUndefined();
  }
  for (const origin of ["http://orders.internal:80", "https://orders.internal:443", "http://[::1]:8080"]) {
    const physical = new URL(origin), req = { host: physical.hostname, protocol: physical.protocol, method: "POST" };
    const headers = { host: ["Host", physical.host] };
    expect(native(req, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, privacy, physical)?.additions.get("traceparent")).toBe(parent);
    expect(native(req, "POST /ordinary HTTP/1.1\r\n", { host: ["Host", "api.openai.com"] }, remote, policy, privacy, physical)).toBeUndefined();
  }
});

test("R3 native staging requires an explicit transport authority", () => {
  const req = { host: "orders.internal", protocol: "http:", method: "POST", port: 80 };
  const headers = { host: ["Host", "orders.internal"] };
  expect(native(req, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, privacy)).toBeUndefined();
  // Neither an invented request.port nor a Host field is transport evidence.
  expect(native(req, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, privacy, undefined)).toBeUndefined();
  for (const origin of ["https://orders.internal", "http://elsewhere.internal", "http://orders.internal:8080"]) {
    expect(native(req, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, privacy, new URL(origin))).toBeUndefined();
  }
});
