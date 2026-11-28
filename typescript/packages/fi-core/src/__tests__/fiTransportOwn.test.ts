import path from "node:path";
import fs from "node:fs";
import { BUILT_GUARD, cleanups, connectProxy, counter, runChild, runCleanups, sink, throwawayCertificate, UNDICI } from "./helpers/realSockets";

/**
 * Key-carrying sends use fi-core's own pinned undici transport and never read the global dispatcher slot or globalThis.fetch. Real sockets,
 * a child process of the built package for every scenario (the application and the package share one global object there), counting
 * listeners at the target, at the proxies and at the places a hostile dispatcher or a replaced fetch would send to.
 */
const KEY = "synthetic-fi-key-0001", SECRET = "synthetic-fi-secret-0001";
const DIST = path.dirname(path.dirname(BUILT_GUARD));
const INDEX = path.join(DIST, "src", "index");
afterEach(async () => { await runCleanups(); });
const cert = throwawayCertificate();
afterAll(() => cert.dispose());
const count = (text: string, pattern: RegExp): number => (text.match(pattern) ?? []).length;
const quiet = (c: { connections: number; bytes: number }) => { expect(c.connections).toBe(0); expect(c.bytes).toBe(0); };

/** The application and the three key-carrying sends (a guarded fetch, an OTLP HTTP export, an eval check), as one child process. */
const CHILD = `
  const net = require("node:net");
  const undici = () => require(${JSON.stringify(UNDICI)});
  const { diag, DiagLogLevel } = require("@opentelemetry/api");
  const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
  const out = (...a) => console.log(a.join(" "));
  diag.setLogger({ verbose: out, debug: out, info: out, warn: out, error: out }, DiagLogLevel.ALL);
  const scenario = process.env.SCENARIO || "";
  const keys = { "x-api-key": ${JSON.stringify(KEY)}, "x-secret-key": ${JSON.stringify(SECRET)} };
  const spy = (label) => { const s = net.connect(Number(process.env.SPY_PORT), "127.0.0.1"); s.on("error", () => {}); s.end(label); };
  const hostile = () => {
    const { Agent, ProxyAgent, setGlobalDispatcher } = undici();
    class Counting extends Agent { dispatch(...args) { out("HOSTILE_DISPATCH"); spy("hostile"); return super.dispatch(...args); } }
    const agent = process.env.PROXY_URL ? new ProxyAgent({ uri: process.env.PROXY_URL, requestTls: { rejectUnauthorized: false } }) : new Counting({ connect: { rejectUnauthorized: false } });
    setGlobalDispatcher(agent); return agent;
  };
  const countingFetch = (real) => async (...args) => { out("REPLACED_FETCH_CALLED"); spy("fetch"); return real(...args); };
  if (scenario === "forged-record") {
    const agent = hostile();
    const record = Object.freeze({ value: agent, fetch: async (...args) => { out("FORGED_FETCH_CALLED"); spy("forged"); return fetch(...args); } });
    Object.defineProperty(globalThis, Symbol.for("traceai.fi-core.node-default-dispatcher"), { value: record, writable: false, configurable: false, enumerable: false });
  }
  if (scenario === "global-agent-before-load") hostile();
  if (scenario === "fetch-replaced-before-load") globalThis.fetch = countingFetch(globalThis.fetch);
  const guard = require(${JSON.stringify(BUILT_GUARD)});
  const core = require(${JSON.stringify(INDEX)});
  if (scenario === "fetch-replaced-after-load") globalThis.fetch = countingFetch(globalThis.fetch);
  if (scenario === "global-agent-after-load") hostile();
  const intended = { keys: new Map(Object.entries(keys)), values: Object.values(keys) };
  const url = process.argv[1];
  const span = () => new Promise((done) => {
    const exporter = core.createSpanExporter({ ...(process.env.OPTION_PROXY ? { httpsProxy: process.env.OPTION_PROXY } : {}), timeoutMillis: 4000 });
    const provider = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(s) { exporter.export([s], (r) => done({ code: r.code, exporter })); }, forceFlush: async () => {}, shutdown: async () => {} }] });
    provider.getTracer("t").startSpan("one").end();
  });
  const tag = { type: "observation_span", value: "llm", eval_name: "Chunk_Attribution", config: {}, custom_eval_name: "Chunk_Attribution", mapping: {}, model: "turing_small", toDict: () => ({ eval_name: "Chunk_Attribution", custom_eval_name: "Chunk_Attribution" }) };
  (async () => {
    for (let i = 0; i < 3; i++) {
      try { const r = await guard.guardedFetch({ url: url + "/fetch", init: { method: "POST", headers: keys }, intended, ...(process.env.OPTION_PROXY ? { httpsProxy: process.env.OPTION_PROXY } : {}) }); out("FETCH " + r.status); }
      catch (e) { out("FETCH REFUSED " + (e.code || e.name)); }
      try { const { code, exporter } = await span(); out("OTLP " + code); await exporter.shutdown(); } catch (e) { out("OTLP THREW " + (e.code || e.name)); }
      try { await core.checkCustomEvalConfigExists("p", [tag], undefined, false, process.env.OPTION_PROXY ? { httpsProxy: process.env.OPTION_PROXY } : {}); out("EVAL DONE"); } catch (e) { out("EVAL THREW " + (e.code || e.name)); }
    }
    out("CHILD DONE");
  })();
`;
const env = (target: { port: number }, extra: Record<string, string> = {}): Record<string, string> => ({
  FI_API_KEY: KEY, FI_SECRET_KEY: SECRET, FI_BASE_URL: `https://localhost:${target.port}`, NODE_EXTRA_CA_CERTS: cert.caFile, ...extra,
});
const received = (target: { requests: Array<{ url: string; headers: Record<string, unknown> }> }) => target.requests.filter(r => r.headers["x-api-key"] === KEY).length;

describe("the transport is fi-core's own", () => {
  test("RED forged record: a frozen record at the shared name and a hostile global dispatcher installed before load: the hostile dispatcher and the record's fetch get 0 calls, the three sends reach the target", async () => {
    const target = await sink({ tls: cert }), spy = await counter(), hostileProxy = await counter();
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { SCENARIO: "forged-record", SPY_PORT: String(spy.port) }));
    expect(output).not.toContain("HOSTILE_DISPATCH"); expect(output).not.toContain("FORGED_FETCH_CALLED");
    quiet(spy); quiet(hostileProxy);
    expect(count(output, /FETCH 200/g)).toBe(3); expect(count(output, /OTLP 0/g)).toBe(3);
    expect(received(target)).toBe(9);
  });
  test("RED forged record with a hostile ProxyAgent (verification off) as the global dispatcher: its proxy sees 0 bytes", async () => {
    const target = await sink({ tls: cert }), spy = await counter(), proxy = await connectProxy(target.port);
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { SCENARIO: "forged-record", SPY_PORT: String(spy.port), PROXY_URL: proxy.url }));
    quiet(proxy); expect(proxy.connects).toHaveLength(0); quiet(spy);
    expect(count(output, /FETCH 200/g)).toBe(3); expect(received(target)).toBe(9);
  });
  test("RED replaced fetch: globalThis.fetch replaced before the package loads, and again after, is never called by a keyed send", async () => {
    for (const scenario of ["fetch-replaced-before-load", "fetch-replaced-after-load"]) {
      const target = await sink({ tls: cert }), spy = await counter();
      const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { SCENARIO: scenario, SPY_PORT: String(spy.port) }));
      expect(output).not.toContain("REPLACED_FETCH_CALLED"); quiet(spy);
      expect(count(output, /FETCH 200/g)).toBe(3); expect(received(target)).toBe(9);
    }
  });
  test("RED agents SDK order: an application dispatcher installed before the package loads, and after, does not stop the sends: they succeed through fi-core's transport", async () => {
    for (const scenario of ["global-agent-before-load", "global-agent-after-load"]) {
      const target = await sink({ tls: cert }), spy = await counter();
      const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { SCENARIO: scenario, SPY_PORT: String(spy.port) }));
      expect(output).not.toContain("FI_DISPATCHER_UNKNOWN"); expect(output).not.toContain("HOSTILE_DISPATCH"); quiet(spy);
      expect(count(output, /FETCH 200/g)).toBe(3); expect(count(output, /OTLP 0/g)).toBe(3);
      expect(received(target)).toBe(9);
    }
  });
  test("RED a caller's dispatcher option is still refused on a keyed send, value-free, with 0 bytes", async () => {
    const target = await sink({ tls: cert });
    const script = `const { Agent } = require(${JSON.stringify(UNDICI)}); const g = require(${JSON.stringify(BUILT_GUARD)});
      const keys = { "x-api-key": ${JSON.stringify(KEY)}, "x-secret-key": ${JSON.stringify(SECRET)} };
      g.guardedFetch({ url: process.argv[1], init: { method: "POST", headers: keys, dispatcher: new Agent() }, intended: { keys: new Map(Object.entries(keys)), values: Object.values(keys) } }).then(r => console.log("STATUS " + r.status), e => console.log("REFUSED " + e.code));`;
    const output = await runChild(script, [`https://localhost:${target.port}/x`], { NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(output).toContain("REFUSED FI_DISPATCHER_UNKNOWN"); quiet(target);
  });
});

describe("verification cannot be disabled", () => {
  const selfSigned = throwawayCertificate();
  afterAll(() => selfSigned.dispose());
  test.each([
    ["NODE_TLS_REJECT_UNAUTHORIZED=0", { NODE_TLS_REJECT_UNAUTHORIZED: "0" }, ""],
    ["a global agent that turns verification off", {}, "global-agent-before-load"],
    ["both", { NODE_TLS_REJECT_UNAUTHORIZED: "0" }, "global-agent-after-load"],
  ])("RED %s against a destination whose certificate is not trusted: every send fails and the destination sees 0 key bytes", async (_label, extra, scenario) => {
    const target = await sink({ tls: selfSigned }), spy = await counter();
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], { FI_API_KEY: KEY, FI_SECRET_KEY: SECRET, FI_BASE_URL: `https://localhost:${target.port}`, SCENARIO: scenario, SPY_PORT: String(spy.port), ...extra });
    expect(count(output, /FETCH 200/g)).toBe(0); expect(count(output, /OTLP 0/g)).toBe(0);
    quiet(target); expect(target.requests).toHaveLength(0); quiet(spy);
  });
});

describe("one message per process", () => {
  const ONE = `
    const { diag, DiagLogLevel } = require("@opentelemetry/api");
    const out = (...a) => console.log("LOG " + a.join(" "));
    diag.setLogger({ verbose() {}, debug() {}, info() {}, warn: out, error: out }, DiagLogLevel.WARN);
    const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
    const load = (dir) => require(dir ? dir + "/index" : ${JSON.stringify(INDEX)});
    const exportOne = (exporter) => new Promise((done) => {
      const p = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(s) { exporter.export([s], (r) => done(r.code)); }, forceFlush: async () => {}, shutdown: async () => {} }] });
      p.getTracer("t").startSpan("one").end();
    });
    (async () => {
      process.env.FI_ALLOW_INSECURE_HTTP = "1";
      const copies = [load(), load(process.env.COPY_DIR)];
      const exporters = copies.map((c) => c.createSpanExporter({ endpoint: process.argv[1], headers: { "x-api-key": ${JSON.stringify(KEY)}, "x-secret-key": ${JSON.stringify(SECRET)} }, timeoutMillis: 1500 }));
      delete process.env.FI_ALLOW_INSECURE_HTTP;
      for (let i = 0; i < 3; i++) console.log("CODE " + await exportOne(exporters[i % 2]));
    })();
  `;
  test("RED three refused OTLP HTTP exports across two copies of the package write exactly one explanatory message; later lines carry only the code", async () => {
    const target = await counter();
    const copy = path.join(DIST, "..", `dist-copy-r9e-${process.pid}`);
    fs.cpSync(path.join(DIST, "src"), path.join(copy, "src"), { recursive: true }); fs.symlinkSync(path.join(DIST, "..", "node_modules"), path.join(copy, "node_modules"));
    cleanups.push(() => fs.rmSync(copy, { recursive: true, force: true }));
    const output = await runChild(ONE, [`http://127.1:${target.port}/v1/traces`], { COPY_DIR: path.join(copy, "src") });
    const lines = output.split("\n").filter(line => line.startsWith("LOG ") && /not sent/.test(line));
    expect(count(output, /CODE 1/g)).toBe(3);
    expect(lines).toHaveLength(3);
    expect(lines.filter(line => /pass allowInsecureHttp: true/.test(line))).toHaveLength(1);
    expect(lines.filter(line => /not sent \(FI_PLAINTEXT_NOT_ALLOWED\)\s*$/.test(line))).toHaveLength(2);
    for (const line of lines) for (const forbidden of [KEY, SECRET, "127.1", String(target.port), "load @traceai/auto first"]) expect(line).not.toContain(forbidden);
    quiet(target);
  });
  test("RED a pre-set latch only hides the message: the refusal and the code line stay", async () => {
    const target = await counter();
    const script = `Object.defineProperty(globalThis, Symbol.for("traceai.fi-core.refusal-message"), { value: true, writable: false, configurable: false, enumerable: false }); ` + ONE.replace('load(process.env.COPY_DIR)', 'load()');
    const output = await runChild(script, [`http://127.1:${target.port}/v1/traces`], {});
    expect(count(output, /CODE 1/g)).toBe(3);
    const lines = output.split("\n").filter(line => line.startsWith("LOG ") && /not sent/.test(line));
    expect(lines).toHaveLength(3);
    for (const line of lines) expect(line).toMatch(/not sent \(FI_PLAINTEXT_NOT_ALLOWED\)\s*$/);
    quiet(target);
  });
});

describe("the SDK-owned https proxy", () => {
  const PROXIED = ["FETCH 200", "OTLP 0", "EVAL DONE"];
  const via = async (extra: Record<string, string>, options: { proxyTls?: boolean } = {}) => {
    const target = await sink({ tls: cert }), spy = await counter();
    const proxy = await connectProxy(target.port, { tls: options.proxyTls ? cert : undefined });
    return { target, spy, proxy };
  };
  test("RED FI_HTTPS_PROXY: a hostile pre-set global dispatcher gets 0 calls, the proxy sees CONNECT and TLS only, and no key or header byte in clear", async () => {
    const { target, spy, proxy } = await via({});
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { SCENARIO: "global-agent-before-load", SPY_PORT: String(spy.port), FI_HTTPS_PROXY: proxy.url }));
    expect(output).not.toContain("HOSTILE_DISPATCH"); quiet(spy);
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    expect(proxy.connects.length).toBeGreaterThanOrEqual(1); expect(target.connections).toBe(proxy.connects.length); for (const connect of proxy.connects) expect(connect).toBe(`localhost:${target.port}`);
    for (const secret of [KEY, SECRET, "x-api-key", "x-secret-key", "POST ", "HTTP/1.1\r\nhost"]) expect(proxy.seen).not.toContain(secret);
    expect(received(target)).toBe(9);
  });
  test("RED NODE_USE_ENV_PROXY=1 with HTTPS_PROXY: the sends tunnel through the proxy; a hostile pre-set slot gets 0 calls", async () => {
    const { target, spy, proxy } = await via({});
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { SCENARIO: "global-agent-before-load", SPY_PORT: String(spy.port), NODE_USE_ENV_PROXY: "1", HTTPS_PROXY: proxy.url }));
    expect(output).not.toContain("HOSTILE_DISPATCH"); quiet(spy);
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    expect(proxy.connects.length).toBeGreaterThanOrEqual(1);
    for (const secret of [KEY, SECRET, "x-api-key"]) expect(proxy.seen).not.toContain(secret);
  });
  test("RED lower-case https_proxy under the switch is read as Node reads it", async () => {
    const { target, proxy } = await via({});
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { NODE_USE_ENV_PROXY: "1", https_proxy: proxy.url }));
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    expect(proxy.connects.length).toBeGreaterThanOrEqual(1);
  });
  test("RED HTTPS_PROXY without the switch is not read: the sends go direct", async () => {
    const { target, proxy } = await via({});
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { HTTPS_PROXY: proxy.url }));
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    quiet(proxy); expect(received(target)).toBe(9);
  });
  test("RED NO_PROXY is honoured for the environment proxy: a destination it names goes direct", async () => {
    const { target, proxy } = await via({});
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { NODE_USE_ENV_PROXY: "1", HTTPS_PROXY: proxy.url, NO_PROXY: "localhost" }));
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    quiet(proxy); expect(received(target)).toBe(9);
  });
  test("RED FI_HTTPS_PROXY wins over HTTPS_PROXY under the switch, and ignores NO_PROXY", async () => {
    const { target, proxy } = await via({}), other = await counter();
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { NODE_USE_ENV_PROXY: "1", HTTPS_PROXY: other.url, FI_HTTPS_PROXY: proxy.url }));
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    expect(proxy.connects.length).toBeGreaterThanOrEqual(1); expect(target.connections).toBe(proxy.connects.length); quiet(other);
  });
  test("RED the httpsProxy option wins over FI_HTTPS_PROXY", async () => {
    const { target, proxy } = await via({}), other = await counter();
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { FI_HTTPS_PROXY: other.url, OPTION_PROXY: proxy.url }));
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    expect(proxy.connects.length).toBeGreaterThanOrEqual(1); expect(target.connections).toBe(proxy.connects.length); quiet(other);
  });
  test("RED an https:// proxy whose own certificate is trusted works; one whose certificate is not trusted is refused with 0 bytes at the target", async () => {
    const trusted = await via({}, { proxyTls: true });
    const ok = await runChild(CHILD, [`https://localhost:${trusted.target.port}`], env(trusted.target, { FI_HTTPS_PROXY: `https://localhost:${trusted.proxy.port}` }));
    for (const line of PROXIED) expect(count(ok, new RegExp(line, "g"))).toBe(3);
    expect(trusted.proxy.connects.length).toBeGreaterThanOrEqual(1);
    const untrusted = throwawayCertificate();
    try {
      const target = await sink({ tls: cert }), proxy = await connectProxy(target.port, { tls: untrusted });
      const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { FI_HTTPS_PROXY: `https://localhost:${proxy.port}` }));
      expect(count(output, /FETCH 200/g)).toBe(0); expect(count(output, /OTLP 0/g)).toBe(0);
      expect(target.requests).toHaveLength(0); quiet(target); expect(proxy.connects).toHaveLength(0);
    } finally { untrusted.dispose(); }
  });
  test("RED a proxy URL that is not http or https is refused, value-free: 0 bytes at the target", async () => {
    const target = await sink({ tls: cert });
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { FI_HTTPS_PROXY: "socks5://user:hunter2@127.0.0.1:1" }));
    expect(count(output, /FETCH REFUSED FI_PROXY_UNSUPPORTED/g)).toBe(3); expect(count(output, /OTLP 1/g)).toBe(3);
    for (const forbidden of ["hunter2", "user:", "socks5://", "127.0.0.1:1"]) expect(output).not.toContain(forbidden);
    quiet(target);
  });
  test("RED a plaintext target with a proxy configured is refused, loopback included: 0 bytes at the target and at the proxy", async () => {
    const target = await sink(), proxy = await counter();
    const output = await runChild(CHILD, [target.url], { FI_API_KEY: KEY, FI_SECRET_KEY: SECRET, FI_BASE_URL: target.url, FI_HTTPS_PROXY: proxy.url });
    expect(count(output, /FETCH REFUSED FI_PLAINTEXT_PROXIED/g)).toBe(3); expect(count(output, /OTLP 1/g)).toBe(3);
    quiet(target); quiet(proxy); expect(target.requests).toHaveLength(0);
  });
  test("RED the proxy URL and its credentials appear in no log, message, error or attribute, and the proxy gets the credentials as Proxy-Authorization only", async () => {
    const { target, proxy } = await via({});
    const credentialed = proxy.url.replace("http://", "http://proxyuser:proxysecret@");
    const output = await runChild(CHILD, [`https://localhost:${target.port}`], env(target, { FI_HTTPS_PROXY: credentialed }));
    for (const line of PROXIED) expect(count(output, new RegExp(line, "g"))).toBe(3);
    for (const forbidden of ["proxyuser", "proxysecret", String(proxy.port), "127.0.0.1", credentialed]) expect(output).not.toContain(forbidden);
    expect(proxy.proxyHeaders.length).toBeGreaterThanOrEqual(1);
    for (const header of proxy.proxyHeaders) expect(header).toBe(`Basic ${Buffer.from("proxyuser:proxysecret").toString("base64")}`);
  });
  test("RED gRPC does not use FI_HTTPS_PROXY: a plaintext loopback export reaches its target directly", async () => {
    const target = await counter(), proxy = await counter();
    const script = `const core = require(${JSON.stringify(INDEX)}); const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
      const exporter = core.createSpanExporter({ transport: "grpc", endpoint: process.argv[1], headers: { "x-api-key": ${JSON.stringify(KEY)}, "x-secret-key": ${JSON.stringify(SECRET)} }, timeoutMillis: 800 });
      const p = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(s) { exporter.export([s], r => { console.log("GRPC " + r.code); process.exit(0); }); }, forceFlush: async () => {}, shutdown: async () => {} }] });
      p.getTracer("t").startSpan("one").end();`;
    await runChild(script, [`http://127.0.0.1:${target.port}`], { FI_HTTPS_PROXY: proxy.url });
    expect(target.connections).toBeGreaterThanOrEqual(1); quiet(proxy);
  });
});

describe("sends that carry no key are not subject to the keyed-send rules", () => {
  const KEYLESS = `
    const core = require(${JSON.stringify(INDEX)});
    const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
    const { diag, DiagLogLevel } = require("@opentelemetry/api");
    diag.setLogger({ verbose() {}, debug() {}, info() {}, warn: (...a) => console.log("LOG " + a.join(" ")), error: (...a) => console.log("LOG " + a.join(" ")) }, DiagLogLevel.WARN);
    const exporter = core.createSpanExporter({ endpoint: process.argv[1], timeoutMillis: 4000 });
    const p = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(s) { exporter.export([s], (r) => { console.log("OTLP " + r.code); process.exit(0); }); }, forceFlush: async () => {}, shutdown: async () => {} }] });
    p.getTracer("t").startSpan("one").end();
  `;
  test("RED a keyless OTLP export to a plaintext collector is sent direct while an SDK proxy or Node's environment-proxy switch is set", async () => {
    for (const extra of [{ FI_HTTPS_PROXY: "PROXY" }, { NODE_USE_ENV_PROXY: "1", HTTPS_PROXY: "PROXY", HTTP_PROXY: "PROXY" }]) {
      const target = await sink(), proxy = await counter();
      const output = await runChild(KEYLESS, [`${target.url}/v1/traces`], Object.fromEntries(Object.entries(extra).map(([k, v]) => [k, v === "PROXY" ? proxy.url : v])));
      expect(count(output, /OTLP 0/g)).toBe(1); expect(target.requests).toHaveLength(1); quiet(proxy);
      expect(output).not.toContain("FI_PLAINTEXT_PROXIED");
    }
  });
  test("RED a keyless OTLP export to an https collector ignores an unsupported FI_HTTPS_PROXY: it is sent, and the proxy setting is never read for it", async () => {
    const target = await sink({ tls: cert });
    const output = await runChild(KEYLESS, [`https://localhost:${target.port}/v1/traces`], { NODE_EXTRA_CA_CERTS: cert.caFile, FI_HTTPS_PROXY: "socks5://127.0.0.1:1" });
    expect(count(output, /OTLP 0/g)).toBe(1); expect(target.requests).toHaveLength(1);
    expect(output).not.toContain("FI_PROXY_UNSUPPORTED");
  });
  test("RED a collector URL with userinfo is sent without it and the userinfo is not logged", async () => {
    const target = await sink();
    const output = await runChild(KEYLESS, [`http://collectoruser:collectorpass@127.0.0.1:${target.port}/v1/traces`], {});
    expect(count(output, /OTLP 0/g)).toBe(1); expect(target.requests).toHaveLength(1);
    expect(target.requests[0].headers.authorization).toBeUndefined();
    expect(output).not.toMatch(/collectoruser|collectorpass/);
  });
});

describe("NO_PROXY for the environment proxy uses the scheme's default port", () => {
  const PROBE = `const guard = require(${JSON.stringify(BUILT_GUARD)}); let r; try { r = guard.resolveProxy(process.argv[1], undefined); } catch (e) { r = "REFUSED " + e.code; }
    console.log("PROXY " + (r === undefined ? "none" : typeof r === "string" && r.startsWith("REFUSED") ? r : "set"));`;
  test.each([
    ["api.example.com:443", "none"], ["api.example.com:80", "set"], ["api.example.com", "none"], [".example.com", "none"], ["other.example.com", "set"], ["*", "none"],
  ])("RED NO_PROXY=%s for https://api.example.com is %s", async (noProxy, expected) => {
    const output = await runChild(PROBE, ["https://api.example.com/x"], { NODE_USE_ENV_PROXY: "1", HTTPS_PROXY: "http://127.0.0.1:1", NO_PROXY: noProxy });
    expect(output).toContain(`PROXY ${expected}`);
  });
});
