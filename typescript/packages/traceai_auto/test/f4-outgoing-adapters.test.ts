import { Agent, ClientRequest, OutgoingMessage, createServer, request as httpRequest } from "node:http";
import type { AddressInfo } from "node:net";
import { channel } from "node:diagnostics_channel";
import { context, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { compileOutgoingPolicy } from "../src/outgoing-policy";
import { createOutgoingPrivacy, type OutgoingPrivacy } from "../src/outgoing-privacy";
import { appendUnsentNativeHeader, installOutgoingPropagation, stageNativeOutgoingHeaders } from "../src/outgoing-node";
import { resolveConfig } from "../src/config";
import type { IntegrationStatus } from "../src/libraries";

// Candidate-only regressions supplement the preserved f4-outgoing-safety assertions.
// Real transport qualification still lives in f4-outgoing-propagation.test.ts.
const policy = compileOutgoingPolicy(undefined, undefined);
const privacy = createOutgoingPrivacy();
const remote = trace.setSpanContext(ROOT_CONTEXT, { traceId: "13579bdf13579bdf13579bdf13579bdf", spanId: "2468ace02468ace0", traceFlags: 0, isRemote: true });
const parent = "00-13579bdf13579bdf13579bdf13579bdf-2468ace02468ace0-00";
const line = "POST /ordinary HTTP/1.1\r\n";
const request = { host: "orders.internal", protocol: "http:", method: "POST" };
function nativeMessage(): any {
  const message = new OutgoingMessage();
  message.setHeader("Host", "orders.internal:8080");
  message.setHeader("X-Duplicate", ["one", "two"]);
  message.setHeader("Content-Length", 6);
  return message;
}
function privateHeaders(message: any): any {
  const key = Object.getOwnPropertySymbols(message).find(key => key.description === "kOutHeaders");
  expect(key).toBeDefined();
  return message[key!];
}

test("R2 native private-map copies serialize as tuples without corrupting Host or framing", () => {
  const original = nativeMessage(), candidate = nativeMessage();
  const headers = privateHeaders(candidate), before = JSON.stringify(headers);
  const staged = stageNativeOutgoingHeaders(request, line, headers, remote, policy, privacy, new URL("http://orders.internal:8080"))!;
  expect(staged).toBeDefined();
  // Use the REAL serializer, not a shape-only mock of the private-map branch.
  original._storeHeader(line, privateHeaders(original));
  candidate._storeHeader(line, staged.headers);
  expect(candidate._header.replace(`traceparent: ${parent}\r\n`, "")).toBe(original._header);
  expect(candidate._header).toContain("Host: orders.internal:8080\r\n");
  expect(candidate._header).toContain("Content-Length: 6\r\n");
  expect(candidate._header).toContain("X-Duplicate: one\r\nX-Duplicate: two\r\n");
  expect(JSON.stringify(headers)).toBe(before);
  expect(candidate.getHeader("traceparent")).toBeUndefined();
});

test.each(["string", "flat"])("HTTP-only opt-in does not subscribe to or mutate Undici %s diagnostics", form => {
  const original = form === "string" ? "x-duplicate: one\r\nx-duplicate: two\r\n" : ["x-duplicate", "one", "x-duplicate", "two"];
  const before = JSON.stringify(original);
  // Model the high-level redirect options retaining the ORIGINAL carrier.
  const first = { origin: "http://orders.internal", path: "/ordinary", method: "POST", host: null, headers: original };
  const denied = { ...first, origin: "http://api.openai.com" };
  const topic = channel("undici:request:create"), statuses: IntegrationStatus[] = [], warn = jest.fn();
  const subscribedBefore = topic.hasSubscribers;
  const active = jest.spyOn(context, "active").mockReturnValue(remote);
  const remove = installOutgoingPropagation(policy, privacy, statuses, warn, true);
  try {
    expect(statuses.map(row => [row.name, row.status])).toEqual([["outgoing-http", "pending"]]);
    topic.publish({ request: first });
    // F4 has no Undici adapter, even when native propagation is opted in.
    expect(first.headers).toBe(original);
    expect(JSON.stringify(original)).toBe(before);
    topic.publish({ request: denied });
    expect(denied.headers).toBe(original);
    expect(topic.hasSubscribers).toBe(subscribedBefore);
    expect(statuses.some(row => row.name === "outgoing-undici")).toBe(false);
    remove(); remove();
    const after = { ...first, headers: original };
    topic.publish({ request: after });
    expect(after.headers).toBe(original);
    expect(warn).not.toHaveBeenCalled();
  } finally { remove(); active.mockRestore(); }
});

test("R2 opted-in native wrapper reports an actual commit and leaves retained foreign wrappers inert", async () => {
  // Real stock-agent options supply the port; an OutgoingMessage-shaped object
  // is no longer sufficient authority evidence for a production adapter.
  const server = createServer((req, res) => { req.resume(); req.on("end", () => res.end()); });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const agent = new Agent({ keepAlive: true });
  const send = () => new Promise<any>((resolve, reject) => {
    const req = httpRequest({ hostname: "127.0.0.1", port: (server.address() as AddressInfo).port, path: "/ordinary", method: "POST", agent }, res => {
      res.resume(); res.on("error", reject); res.on("end", () => resolve(req));
    });
    req.on("error", reject); req.end("body");
  });
  const proto: any = ClientRequest.prototype;
  const before = Object.getOwnPropertyDescriptor(proto, "_storeHeader");
  const statuses: IntegrationStatus[] = [], warn = jest.fn();
  const active = jest.spyOn(context, "active").mockReturnValue(remote);
  const remove = installOutgoingPropagation(policy, privacy, statuses, warn, true);
  try {
    const owned = proto._storeHeader;
    const first = await send();
    expect(first._header).toContain(`traceparent: ${parent}\r\n`);
    expect(statuses.find(row => row.name === "outgoing-http")?.status).toBe("instrumented");
    const foreign = function(this: unknown, ...args: unknown[]) { return Reflect.apply(owned, this, args); };
    Object.defineProperty(proto, "_storeHeader", { configurable: true, writable: true, value: foreign });
    remove(); remove();
    expect(proto._storeHeader).toBe(foreign);
    const after = await send();
    expect(after._header).not.toContain("traceparent:");
    expect(warn).not.toHaveBeenCalled();
  } finally {
    remove(); active.mockRestore();
    if (before) Object.defineProperty(proto, "_storeHeader", before); else delete proto._storeHeader;
    agent.destroy(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve()));
  }
});

test("R2 opt-in cannot override none or an invalid destination policy", () => {
  for (const denied of [compileOutgoingPolicy("none", "+orders.internal"), compileOutgoingPolicy("all", "-bad/path")]) {
    const before = Object.getOwnPropertyDescriptor(ClientRequest.prototype, "_storeHeader");
    const statuses: IntegrationStatus[] = [];
    const remove = installOutgoingPropagation(denied, privacy, statuses, jest.fn(), true);
    try {
      expect(statuses.map(row => row.status)).toEqual(["disabled"]);
      expect(Object.getOwnPropertyDescriptor(ClientRequest.prototype, "_storeHeader")).toEqual(before);
    } finally { remove(); }
  }
});

test("R2 opted-in native host failure is delegated exactly once with the original error", () => {
  const proto: any = ClientRequest.prototype;
  const previous = Object.getOwnPropertyDescriptor(proto, "_storeHeader");
  const error = new Error("synthetic host failure");
  const original = jest.fn(() => { throw error; });
  Object.defineProperty(proto, "_storeHeader", { configurable: true, writable: true, value: original });
  const active = jest.spyOn(context, "active").mockReturnValue(remote);
  const broken: OutgoingPrivacy = { prepareOutgoingHeaders() { throw new Error("synthetic staging failure"); } };
  const remove = installOutgoingPropagation(policy, broken, [], jest.fn(), true);
  try {
    const headers = { host: ["Host", "orders.internal"] };
    let thrown: unknown;
    try { Reflect.apply(proto._storeHeader, request, [line, headers]); } catch (caught) { thrown = caught; }
    expect(thrown).toBe(error);
    expect(original).toHaveBeenCalledTimes(1);
    expect(original.mock.calls[0]).toEqual([line, headers]);
  } finally {
    remove(); active.mockRestore();
    if (previous) Object.defineProperty(proto, "_storeHeader", previous); else delete proto._storeHeader;
  }
});

test("R2 adapter opt-in is strict and resolves code before environment before file", () => {
  const dir = mkdtempSync(join(tmpdir(), "f4-adapters-"));
  mkdirSync(join(dir, ".fi")); writeFileSync(join(dir, ".fi/config.toml"), "");
  const file = join(dir, "futureagi.toml");
  const cwd = jest.spyOn(process, "cwd").mockReturnValue(dir);
  const env = { HOME: dir, XDG_CONFIG_HOME: dir };
  try {
    writeFileSync(file, "");
    expect(resolveConfig({ configFile: file }, env).propagateOutgoingAdapters).toBe(false);
    writeFileSync(file, "propagate_outgoing_adapters = true\n");
    expect(resolveConfig({ configFile: file }, env).propagateOutgoingAdapters).toBe(true);
    expect(resolveConfig({ configFile: file }, { ...env, TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: "false" }).propagateOutgoingAdapters).toBe(false);
    expect(resolveConfig({ configFile: file, propagateOutgoingAdapters: true }, { ...env, TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: "false" }).propagateOutgoingAdapters).toBe(true);
    expect(resolveConfig({ configFile: file, propagateOutgoingAdapters: false }, { ...env, TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: "true" }).propagateOutgoingAdapters).toBe(false);
    for (const raw of ["", "TRUE", "1", "invalid", "false"]) {
      expect(resolveConfig({ configFile: file }, { ...env, TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: raw }).propagateOutgoingAdapters).toBe(false);
    }
    for (const raw of ["[broken", "1", "", "false", "{}", "[]"]) {
      writeFileSync(file, `propagate_outgoing_adapters = ${raw}\n`);
      expect(resolveConfig({ configFile: file }, env).propagateOutgoingAdapters).toBe(false);
    }
  } finally { cwd.mockRestore(); rmSync(dir, { recursive: true, force: true }); }
});

test("R3 native commit without captured transport authority stays unchanged", () => {
  const proto: any = ClientRequest.prototype;
  const getter = jest.fn(() => 8080);
  const message = Object.defineProperty(Object.assign(nativeMessage(), request), "port", { get: getter });
  const statuses: IntegrationStatus[] = [];
  const active = jest.spyOn(context, "active").mockReturnValue(remote);
  const remove = installOutgoingPropagation(policy, privacy, statuses, jest.fn(), true);
  try {
    Reflect.apply(proto._storeHeader, message, [line, privateHeaders(message)]);
    expect(message._header).not.toContain("traceparent:");
    expect(message._header).toContain("Host: orders.internal:8080\r\n");
    expect(getter).not.toHaveBeenCalled();
    expect(statuses.find(row => row.name === "outgoing-http")?.status).toBe("pending");
  } finally { remove(); active.mockRestore(); }
});

test("R3 foreign agent entry points fail closed and retain original errors", () => {
  const proto: any = Agent.prototype, previous = Object.getOwnPropertyDescriptor(proto, "addRequest")!;
  const error = new Error("synthetic agent failure"), original = jest.fn(() => { throw error; });
  Object.defineProperty(proto, "addRequest", { ...previous, value: original });
  const agent = new Agent(), req = { host: "orders.internal", protocol: "http:", method: "POST", agent };
  const options = { host: "orders.internal", port: 8080 };
  const statuses: IntegrationStatus[] = [], warn = jest.fn();
  const remove = installOutgoingPropagation(policy, privacy, statuses, warn, true);
  try {
    expect(statuses.find(row => row.name === "outgoing-http")?.status).toBe("failed");
    expect(proto.addRequest).toBe(original);
    expect(warn).toHaveBeenCalledTimes(1);
    const owned = proto.addRequest;
    let thrown: unknown;
    try { Reflect.apply(owned, agent, [req, options]); } catch (caught) { thrown = caught; }
    expect(thrown).toBe(error);
    expect(original).toHaveBeenCalledTimes(1);
    expect(original.mock.calls[0]).toEqual([req, options]);
    const foreign = function(this: unknown, ...args: unknown[]) { return Reflect.apply(owned, this, args); };
    Object.defineProperty(proto, "addRequest", { ...previous, value: foreign });
    remove(); remove();
    expect(proto.addRequest).toBe(foreign);
    expect(() => Reflect.apply(foreign, agent, [req, options])).toThrow(error);
    expect(original).toHaveBeenCalledTimes(2);
  } finally { remove(); Object.defineProperty(proto, "addRequest", previous); agent.destroy(); }
});

function unsentExpect(): { req: any; agent: Agent } {
  const agent = new Agent();
  // Helper-only test: construct Node's real Expect queue without any socket.
  (agent as any).addRequest = () => {};
  const req = httpRequest({ host: "orders.internal", port: 8080, method: "POST", headers: { Expect: "100-continue" }, agent });
  req.on("error", () => {});
  return { req, agent };
}
test("R4 Expect queue append preserves exact bytes, slot identity, callback and accounting", () => {
  const { req, agent } = unsentExpect();
  try {
    const header = req._header, slot = req.outputData[0], callback = slot.callback, encoding = slot.encoding;
    const suffix = `traceparent: ${parent}\r\n`;
    expect(req._headerSent).toBe(true);
    expect(req.outputData).toHaveLength(1);
    expect(slot.data).toBe(header);
    expect(appendUnsentNativeHeader(req, header, suffix)).toBe(true);
    expect(req._header.replace(suffix, "")).toBe(header);
    expect(req.outputData[0]).toBe(slot);
    expect(slot.data).toBe(req._header);
    expect(slot.callback).toBe(callback);
    expect(slot.encoding).toBe(encoding);
    expect(req.outputSize).toBe(req._header.length);
    expect(req.getHeader("traceparent")).toBeUndefined();
    expect(appendUnsentNativeHeader(req, header, suffix)).toBe(false);
  } finally { req.destroy(); agent.destroy(); }
});
test.each(["body", "changed", "size", "callback", "frozen"])("R4 unknown Expect queue %s stays byte-identical", change => {
  const { req, agent } = unsentExpect();
  try {
    const header = req._header;
    if (change === "body") req.outputData.push({ data: Buffer.from([0, 255]), encoding: undefined, callback: undefined });
    if (change === "changed") req.outputData[0].data += "body";
    if (change === "size") req.outputSize++;
    if (change === "callback") req._onPendingData = jest.fn();
    if (change === "frozen") Object.freeze(req.outputData[0]);
    const before = JSON.stringify({ header: req._header, output: req.outputData, size: req.outputSize });
    expect(appendUnsentNativeHeader(req, header, `traceparent: ${parent}\r\n`)).toBe(false);
    expect(JSON.stringify({ header: req._header, output: req.outputData, size: req.outputSize })).toBe(before);
    if (change === "callback") expect(req._onPendingData).not.toHaveBeenCalled();
  } finally { req.destroy(); agent.destroy(); }
});

// Unsupported transports are tested only for absence of agent additions. This
// does not claim fetch/Undici propagation support or runtime qualification.
test.each(["fetch", "undici-fetch", "undici-request"])("HTTP-only F4 leaves %s uninstrumented with adapters enabled", async transport => {
  const received: Record<string, unknown>[] = [];
  const server = createServer((req, res) => { received.push(req.headers); req.resume(); req.on("end", () => res.end("ok")); });
  await new Promise<void>((ready, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", ready); });
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/ordinary`;
  const undici = transport === "fetch" ? undefined : require("undici");
  const originalFetch = globalThis.fetch, originalUndiciFetch = undici?.fetch, originalUndiciRequest = undici?.request;
  const statuses: IntegrationStatus[] = [], warn = jest.fn();
  const active = jest.spyOn(context, "active").mockReturnValue(remote);
  const remove = installOutgoingPropagation(policy, privacy, statuses, warn, true);
  try {
    expect(globalThis.fetch).toBe(originalFetch);
    if (undici) { expect(undici.fetch).toBe(originalUndiciFetch); expect(undici.request).toBe(originalUndiciRequest); }
    if (transport === "undici-request") {
      const response = await undici.request(url); expect(await response.body.text()).toBe("ok");
    } else {
      const response = await (undici?.fetch ?? globalThis.fetch)(url); expect(await response.text()).toBe("ok");
    }
    expect(received).toHaveLength(1);
    for (const name of ["traceparent", "tracestate", "baggage", "x-private-custom"]) expect(received[0][name]).toBeUndefined();
    expect(statuses.map(row => [row.name, row.status])).toEqual([["outgoing-http", "pending"]]);
    expect(warn).not.toHaveBeenCalled();
  } finally { remove(); active.mockRestore(); server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); }
});
