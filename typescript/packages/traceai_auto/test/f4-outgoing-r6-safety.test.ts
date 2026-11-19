import { ROOT_CONTEXT, trace, propagation, baggageEntryMetadataFromString } from "@opentelemetry/api";
import { Agent, request as httpRequest } from "node:http";
import { compileOutgoingPolicy, allowsOutgoingDestination } from "../src/outgoing-policy";
import { createOutgoingPrivacy, type OutgoingPrivacy } from "../src/outgoing-privacy";
import { appendUnsentNativeHeader, stageNativeOutgoingHeaders } from "../src/outgoing-node";
import { loadCatalog } from "../src/wire/core";

// R-LIT: synthetic W3C identity, not a credential (F4-R6-context).
const remote = trace.setSpanContext(ROOT_CONTEXT, { traceId: "13579bdf13579bdf13579bdf13579bdf", spanId: "2468ace02468ace0", traceFlags: 0, isRemote: true });
const parent = "00-13579bdf13579bdf13579bdf13579bdf-2468ace02468ace0-00";
const policy = compileOutgoingPolicy(undefined, undefined), privacy = createOutgoingPrivacy();
const destination = new URL("http://orders.internal:8080/ordinary");
const prepare = (entries: Parameters<typeof propagation.createBaggage>[0]) => privacy.prepareOutgoingHeaders(propagation.setBaggage(remote, propagation.createBaggage(entries)), destination, [], policy);

test("R6 catalog host and port veto is unconditional for every policy mode and rule form", () => {
  for (const provider of loadCatalog().providers) {
    const hosts = [...(provider.hosts ?? []), ...(provider.host_suffixes ?? []).map(suffix => provider.match === "all" ? `${provider.host_prefixes![0]}us-test${suffix}` : `tenant${suffix}`)];
    const targets = [...hosts.map(host => [host, 443] as const), ...(provider.ports ?? []).map(port => ["orders.internal", port] as const)];
    for (const [host, port] of targets) for (const protocol of ["http", "https"]) for (const mode of ["default", "all", "none"]) {
      for (const rules of [undefined, `+${host}`, host, `+${host},-${host}`]) {
        expect(allowsOutgoingDestination(compileOutgoingPolicy(mode, rules), new URL(`${protocol}://${host}:${port}/health`))).toBe(false);
      }
    }
  }
  for (const mode of ["default", "all"]) {
    expect(allowsOutgoingDestination(compileOutgoingPolicy(mode, "+orders.internal"), destination)).toBe(true);
    expect(allowsOutgoingDestination(compileOutgoingPolicy(mode, "+orders.internal"), new URL("http://other.internal"))).toBe(false);
  }
});

test.each([
  "note=čĊx-probe:yes", "note=\u0085", "note=west\r\nx-probe:yes", "note=we\nst",
  "note= ", "note= ", "note=é", "note=\"quoted\"", "note=back\\slash",
  "note=bad%", "note=%0d%0a", "note=good;badč=value", "note=ok,other=value",
])("R6 invalid baggage metadata is omitted without losing safe siblings/parent: %j", metadata => {
  const result = prepare({ region: { value: "west" }, probe: { value: "safe", metadata: baggageEntryMetadataFromString(metadata) } });
  expect(result?.get("traceparent")).toBe(parent);
  expect(result?.get("baggage")).toBe("region=west");
});

test("R6 W3C encoding keeps Unicode values ASCII and accepts safe metadata", () => {
  const result = prepare({ region: { value: "west", metadata: baggageEntryMetadataFromString("note=%C3%A9;flag") }, city: { value: "Zürich" } });
  expect(result?.get("traceparent")).toBe(parent);
  expect(result?.get("baggage")).toBe("region=west;note=%C3%A9;flag,city=Z%C3%BCrich");
  expect(result?.get("baggage")).toMatch(/^[\x20-\x7e]+$/);
});

test.each(["a(b)", "a:b", "a/b", "a=b", "a,b", "a;b"])("R6 encoded baggage key must be a token: %s", key => {
  const result = prepare({ region: { value: "west" }, [key]: { value: "safe" } });
  // encodeURIComponent escapes punctuation except parentheses; the encoded form
  // must be a W3C token. Other listed keys are safely percent-encoded.
  expect(result?.get("traceparent")).toBe(parent);
  expect(result?.get("baggage")).toBe(key === "a(b)" ? "region=west" : `region=west,${encodeURIComponent(key)}=safe`);
});

test.each([
  ["baggage", "probe=safe;note=čĊx-probe:yes"],
  ["baggage", "probe=safe;note=\"quoted\""],
  ["tracestate", "region=west\r\nx-probe:yes"],
  ["traceparent", parent + "Ċ"],
  ["x-probe", "yes"],
])("R6 native commit validates every staged field, including privacy substitutes: %s", (name, value) => {
  const substitute: OutgoingPrivacy = { prepareOutgoingHeaders() { return new Map([[name, value]]) as any; } };
  const headers = { host: ["Host", destination.host] }, before = JSON.stringify(headers);
  expect(stageNativeOutgoingHeaders({ host: destination.hostname, protocol: destination.protocol, method: "POST" }, "POST /ordinary HTTP/1.1\r\n", headers, remote, policy, substitute, new URL(destination.origin))).toBeUndefined();
  expect(JSON.stringify(headers)).toBe(before);
});

test.each(["baggage: probe=safe;note=čĊx-probe:yes\r\n", "baggage: probe=safe\r\nx-probe: yes\r\n", "baggage: probe=\"quoted\"\r\n"])("R6 early append rejects a non-W3C suffix without mutating the Expect queue", suffix => {
  const agent = new Agent(); (agent as any).addRequest = () => {};
  const req: any = httpRequest({ host: "orders.internal", port: 8080, method: "POST", headers: { Expect: "100-continue" }, agent });
  req.on("error", () => {});
  try {
    const before = JSON.stringify({ header: req._header, queue: req.outputData, size: req.outputSize });
    expect(appendUnsentNativeHeader(req, req._header, suffix)).toBe(false);
    expect(JSON.stringify({ header: req._header, queue: req.outputData, size: req.outputSize })).toBe(before);
  } finally { req.destroy(); agent.destroy(); }
});
