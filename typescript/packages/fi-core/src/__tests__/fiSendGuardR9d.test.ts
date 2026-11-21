import dns from "node:dns";
import fs from "node:fs";
import path from "node:path";
import { ExportResultCode } from "@opentelemetry/core";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter } from "../otel";
import { guardedFetch } from "../fi-send-guard";
import { BUILT_GUARD, cleanups, connectProxy, counter, runChild, runCleanups, sink, throwawayCertificate, UNDICI } from "./helpers/realSockets";

/**
 * One value-free message per process when an application dispatcher refuses key-carrying exports, a dispatcher installed before load is not
 * trusted under Node's environment-proxy switch, the switch is read the way Node reads it, a pre-seeded shared record and a replaced global fetch
 * are not trusted, and a lone Future AGI key header makes a send key-carrying. Real sockets throughout: 0 bytes where a send is refused.
 */
const KEY = "synthetic-fi-key-0001", SECRET = "synthetic-fi-secret-0001";
const FIX = "node --import @traceai/auto/register app.mjs";
const DIST = path.dirname(BUILT_GUARD);
afterEach(async () => { await runCleanups(); });
const count = (text: string, pattern: RegExp): number => (text.match(pattern) ?? []).length;
const nothing = (c: { connections: number; bytes: number }) => { expect(c.connections).toBe(0); expect(c.bytes).toBe(0); };

/** A child of the built package. SCENARIO picks what the application does before and after the package loads. */
const CHILD = `
  const slot = Symbol.for("undici.globalDispatcher.1");
  const keys = { "x-api-key": ${JSON.stringify(KEY)}, "x-secret-key": ${JSON.stringify(SECRET)} };
  const intended = { keys: new Map(Object.entries(keys)), values: Object.values(keys) };
  const undici = () => require(${JSON.stringify(UNDICI)});
  const { diag, DiagLogLevel } = require("@opentelemetry/api");
  const log = (...a) => console.log("DIAG " + a.join(" "));
  diag.setLogger({ verbose() {}, debug() {}, info() {}, warn: log, error: log }, DiagLogLevel.WARN);
  const scenario = process.env.SCENARIO;
  const load = (dir) => require(dir ? dir + "/fi-send-guard" : ${JSON.stringify(BUILT_GUARD)});
  async function send(guard, url, init = {}) {
    try { const r = await guard.guardedFetch({ url, init: { method: "POST", headers: keys, ...init }, intended }); console.log("STATUS " + r.status); }
    catch (e) { console.log("REFUSED " + e.code); }
  }
  (async () => {
    const url = process.argv[1];
    if (process.env.RUNTIME_SWITCH) process.env.NODE_USE_ENV_PROXY = "1";
    if (scenario === "app-agent-before-load" || scenario === "two-copies") { const { Agent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new Agent()); }
    if (scenario === "proxy-agent-before-load") { const { ProxyAgent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new ProxyAgent(process.env.PROXY_URL, { requestTls: { rejectUnauthorized: false } })); }
    if (scenario === "preseeded-record") {
      const { Agent, setGlobalDispatcher } = undici(); const agent = new Agent(); setGlobalDispatcher(agent);
      globalThis[Symbol.for("traceai.fi-core.node-default-dispatcher")] = { value: agent };
    }
    const guard = load();
    if (scenario === "late-runtime-switch") process.env.NODE_USE_ENV_PROXY = "1";
    if (scenario === "replaced-fetch") {
      const net = require("node:net"); const real = globalThis.fetch;
      globalThis.fetch = async (...args) => { console.log("REPLACEMENT_FETCH_CALLED"); const s = net.connect(Number(process.env.SPY_PORT), "127.0.0.1"); s.on("error", () => {}); s.end("spy:" + JSON.stringify(args[1] && args[1].headers)); return real(...args); };
    }
    if (scenario === "app-agent-before-load" || scenario === "preseeded-record" || scenario === "proxy-agent-before-load" || scenario === "no-dispatcher" || scenario === "replaced-fetch" || scenario === "late-runtime-switch") {
      for (let i = 0; i < 3; i++) await send(guard, url);
    }
    if (scenario === "two-copies") {
      const copy = load(process.env.COPY_DIR);
      for (const g of [guard, copy, guard, copy]) await send(g, url);
    }
    if (scenario === "caller-option") { const { Agent } = undici(); const a = new Agent(); await send(guard, url, { dispatcher: a }); await send(guard, url, { dispatcher: a }); }
    if (scenario === "caller-option-after-slot-refusal") {
      const { Agent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new Agent());
      const g = require(${JSON.stringify(BUILT_GUARD)});
      await send(g, url, { dispatcher: new Agent() });
    }
  })();
`;
const messages = (output: string): string[] => output.split("\n").filter(line => line.startsWith("DIAG ") && /dispatcher/i.test(line) && /@traceai\/auto/.test(line));

describe("one value-free message per process when a dispatcher installed before load refuses key-carrying exports", () => {
  test("RED a: a global dispatcher installed before the package loads, then 3 exports: refused each time, the message appears exactly once and names the load-order fix", async () => {
    const target = await sink();
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "app-agent-before-load" });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(3);
    const found = messages(output);
    expect(found).toHaveLength(1);
    expect(found[0]).toContain(FIX);
    expect(found[0]).toMatch(/another global dispatcher/i);
    nothing(target); expect(target.requests).toHaveLength(0);
  });
  test("RED b: two copies of the package in one process: the message still appears exactly once", async () => {
    const target = await sink();
    const copy = path.join(DIST, "..", "..", `dist-copy-${process.pid}`);
    fs.cpSync(path.join(DIST, ".."), copy, { recursive: true }); cleanups.push(() => fs.rmSync(copy, { recursive: true, force: true }));
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "two-copies", COPY_DIR: path.join(copy, "src") });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(4);
    expect(messages(output)).toHaveLength(1);
    nothing(target);
  });
  test("c: a caller's dispatcher option keeps its own refusal and emits no load-order message", async () => {
    const target = await sink();
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "caller-option" });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(2);
    expect(messages(output)).toHaveLength(0);
    expect(output).not.toContain(FIX);
    nothing(target);
    await expect(guardedFetch({ url: `${target.url}/x`, init: { method: "POST", headers: { "x-api-key": KEY, "x-secret-key": SECRET }, dispatcher: {} as any },
      intended: { keys: new Map([["x-api-key", KEY], ["x-secret-key", SECRET]]), values: [KEY, SECRET] } })).rejects.toThrow(/Node's default transport/);
  });
  test("RED d: the message holds no URL, host, port, header name, header value or key", async () => {
    const target = await sink();
    const output = await runChild(CHILD, [`${target.url}/x?token=abc`], { SCENARIO: "app-agent-before-load" });
    const found = messages(output);
    expect(found).toHaveLength(1);
    for (const forbidden of [KEY, SECRET, "x-api-key", "x-secret-key", "127.0.0.1", String(target.port), "http://", "https://", "token=abc", "/x"]) expect(found[0]).not.toContain(forbidden);
    nothing(target);
  });
});

describe("a dispatcher installed before load is not Node's own, under NODE_USE_ENV_PROXY too", () => {
  const cert = throwawayCertificate(); afterAll(() => cert.dispose());
  test("RED P1-a: NODE_USE_ENV_PROXY=1 and an application ProxyAgent installed before load, https target: refused, the proxy and the target see 0 bytes", async () => {
    const approved = await sink({ tls: cert }), proxy = await counter();
    const output = await runChild(CHILD, [`https://localhost:${approved.port}/x`], { SCENARIO: "proxy-agent-before-load", PROXY_URL: proxy.url, NODE_USE_ENV_PROXY: "1", NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(3);
    nothing(proxy); nothing(approved);
  });
  test("RED P1-a: --use-env-proxy and an application ProxyAgent installed before load: refused, 0 bytes", async () => {
    const approved = await sink({ tls: cert }), proxy = await counter();
    const output = await runChild(CHILD, [`https://localhost:${approved.port}/x`], { SCENARIO: "proxy-agent-before-load", PROXY_URL: proxy.url, NODE_OPTIONS: "--use-env-proxy", NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(3);
    nothing(proxy); nothing(approved);
  });
  test("RED P1-a: NODE_USE_ENV_PROXY set at runtime, then an application dispatcher, then the package loads: refused, 0 bytes", async () => {
    const approved = await sink({ tls: cert }), proxy = await counter();
    const output = await runChild(CHILD, [`https://localhost:${approved.port}/x`], { SCENARIO: "proxy-agent-before-load", RUNTIME_SWITCH: "1", PROXY_URL: proxy.url, NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(3);
    nothing(proxy); nothing(approved);
  });
  test("RED P1-a: a pre-set slot is refused the same way with the switch on and with it off", async () => {
    const target = await sink();
    for (const env of [{ NODE_USE_ENV_PROXY: "1" } as Record<string, string>, {} as Record<string, string>]) {
      const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "app-agent-before-load", ...env });
      expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(3);
    }
    nothing(target);
  });
  test("RED switch: only the value 1 turns Node's environment proxy on (as Node reads it): NODE_USE_ENV_PROXY=true leaves a plaintext send direct to the target, the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "no-dispatcher", NODE_USE_ENV_PROXY: "true", HTTP_PROXY: proxy.url });
    expect(count(output, /STATUS 200/g)).toBe(3);
    expect(target.requests).toHaveLength(3); nothing(proxy);
  });
  test("RED switch: a value set at runtime after the package loaded does not turn it on: the plaintext send goes direct, the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "late-runtime-switch", HTTP_PROXY: proxy.url });
    expect(count(output, /STATUS 200/g)).toBe(3);
    expect(target.requests).toHaveLength(3); nothing(proxy);
  });
});

describe("the shared record and the global fetch are not trusted", () => {
  test("RED P2-a: a record pre-seeded at the shared symbol, then an application dispatcher: refused, the target sees 0 bytes", async () => {
    const target = await sink();
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "preseeded-record" });
    expect(count(output, /REFUSED FI_DISPATCHER_UNKNOWN/g)).toBe(3);
    nothing(target);
  });
  test("RED P2-b: a global fetch replaced after load is never called: the replacement gets 0 calls and its listener 0 bytes; the send goes through Node's own fetch", async () => {
    const target = await sink(), spy = await counter();
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "replaced-fetch", SPY_PORT: String(spy.port) });
    expect(output).not.toContain("REPLACEMENT_FETCH_CALLED");
    nothing(spy);
    expect(count(output, /STATUS 200/g)).toBe(3);
    expect(target.requests).toHaveLength(3);
  });
});

describe("a lone Future AGI key header makes a send key-carrying", () => {
  const lookup = dns.lookup;
  afterEach(() => { (dns as any).lookup = lookup; });
  const shim = () => { (dns as any).lookup = (_host: string, options: any, callback?: any) => { const done = typeof options === "function" ? options : callback; process.nextTick(() => (options && options.all) ? done(null, [{ address: "127.0.0.1", family: 4 }]) : done(null, "127.0.0.1", 4)); }; };
  const saved: Record<string, string | undefined> = {};
  beforeEach(() => { for (const n of ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_ALLOW_INSECURE_HTTP"]) { saved[n] = process.env[n]; delete process.env[n]; } });
  afterEach(() => { for (const n of Object.keys(saved)) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });
  test.each(["x-api-key", "x-secret-key"])("RED P1-b: a lone %s in gRPC metadata with any value, to a plaintext non-FI non-loopback host without the opt-in: refused, a counting listener sees 0 bytes", async name => {
    const target = await counter(); shim();
    let exporter: any;
    try { exporter = createSpanExporter({ transport: "grpc" as any, endpoint: `http://127.1:${target.port}`, headers: { [name]: "any-other-value-123456" }, timeoutMillis: 800 } as any); }
    catch (error) { expect(error).toEqual(expect.objectContaining({ code: "FI_PLAINTEXT_NOT_ALLOWED" })); }
    // A refused exporter is never built; one that was built must fail its export. Either way nothing is sent.
    if (exporter) {
      const done = await new Promise<number>(resolve => {
        const provider = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(span: any) { exporter.export([span], (r: { code: number }) => resolve(r.code)); }, forceFlush: async () => {}, shutdown: async () => {} } as any] });
        provider.getTracer("t").startSpan("one").end();
      });
      expect(done).toBe(ExportResultCode.FAILED);
    }
    expect(exporter).toBeUndefined();
    nothing(target);
  });
  test.each(["x-api-key", "X-Secret-Key"])("RED P1-b: a lone %s with a value that is not an FI key, to a plaintext non-FI non-loopback host over HTTP without the opt-in: refused, 0 bytes", async name => {
    const target = await sink(); shim();
    await expect(guardedFetch({ url: `http://127.1:${target.port}/x`, init: { method: "POST", headers: { [name]: "any-other-value-123456" } },
      intended: { keys: new Map([[name.toLowerCase(), "any-other-value-123456"]]), values: ["any-other-value-123456"] } })).rejects.toEqual(expect.objectContaining({ code: "FI_PLAINTEXT_NOT_ALLOWED" }));
    nothing(target);
  });
});
void connectProxy;
