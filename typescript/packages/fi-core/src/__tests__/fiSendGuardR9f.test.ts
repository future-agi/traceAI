import path from "node:path";
import { spawnSync } from "node:child_process";
import { BUILT_GUARD, counter, runChild, runCleanups, sink, throwawayCertificate } from "./helpers/realSockets";

/**
 * Loading fi-core touches nothing of the process-wide undici slot (the property `undici.globalDispatcher.1` of the global object): it is
 * neither read (a getter the application put there never runs) nor written (an empty slot stays empty, a data slot keeps its value), and the
 * application's own fetch is dispatched by Node's own undici. One explanation of a refusal per process is shared by the log line and the
 * error the export returns. Real sockets and child processes of the built package throughout.
 */
const DIST = path.dirname(BUILT_GUARD);
const INDEX = path.join(DIST, "index");
const UNDICI_6 = path.dirname(require.resolve("undici"));
afterEach(async () => { await runCleanups(); });

/** A child that loads the built guard with the slot prepared by MODE, then reports the slot, sends once keyed to its own server, and sends the application's fetch. */
const SLOT = `
  const http = require("node:http");
  const slot = Symbol.for("undici.globalDispatcher.1");
  const mode = process.env.MODE; let ran = 0;
  const getter = () => { ran++; return undefined; };
  const marker = { dispatch() { return true; } };
  if (mode === "getter") Object.defineProperty(globalThis, slot, { configurable: true, get: getter });
  if (mode === "data") Object.defineProperty(globalThis, slot, { configurable: true, writable: true, value: marker });
  const state = () => { const d = Object.getOwnPropertyDescriptor(globalThis, slot);
    return "getterRan=" + ran + " descriptor=" + (!d ? "absent" : d.get ? (d.get === getter ? "same-getter" : "other-getter") : d.value === marker ? "same-data" : "other-data"); };
  const guard = require(process.env.TARGET);
  console.log("LOADED " + state());
  (async () => {
    const seen = [];
    const server = http.createServer((req, res) => { seen.push(req.headers["x-api-key"] ? "keyed" : "plain"); res.end("ok"); });
    await new Promise(done => server.listen(0, "127.0.0.1", done));
    const url = "http://127.0.0.1:" + server.address().port + "/x";
    if (typeof guard.guardedFetch === "function") {
      const keys = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
      const r = await guard.guardedFetch({ url, init: { method: "POST", headers: keys }, intended: { keys: new Map(Object.entries(keys)), values: Object.values(keys) } });
      console.log("KEYED_STATUS " + r.status);
      console.log("SENT " + state());
    }
    if (mode === "empty") {
      const r = await fetch(url);
      console.log("APP_FETCH_STATUS " + r.status);
      const d = Object.getOwnPropertyDescriptor(globalThis, slot);
      const Agent6 = require(process.env.UNDICI_6).Agent;
      console.log("APP_DISPATCHER_IS_BUNDLED_UNDICI " + Boolean(d && d.value instanceof Agent6));
    }
    console.log("SEEN " + seen.join(","));
    server.close(); process.exit(0);
  })();
`;
const run = (mode: string): string => spawnSync(process.execPath, ["-e", SLOT], { encoding: "utf8", timeout: 30000, env: { PATH: process.env.PATH ?? "", MODE: mode, TARGET: BUILT_GUARD, UNDICI_6 } }).stdout;

describe("loading fi-core leaves the undici global slot untouched", () => {
  test("RED a getter the application put on the slot before load never runs, and is still the slot's own getter after load and after a keyed send", () => {
    const output = run("getter");
    expect(output).toContain("LOADED getterRan=0 descriptor=same-getter");
    expect(output).toContain("KEYED_STATUS 200");
    expect(output).toContain("SENT getterRan=0 descriptor=same-getter");
  });
  test("a data property on the slot keeps its own value after load and after a keyed send", () => {
    const output = run("data");
    expect(output).toContain("LOADED getterRan=0 descriptor=same-data");
    expect(output).toContain("SENT getterRan=0 descriptor=same-data");
  });
  test("RED an empty slot stays absent after load and after a keyed send, and the application's own fetch runs on Node's undici, not on the bundled one", () => {
    const output = run("empty");
    expect(output).toContain("LOADED getterRan=0 descriptor=absent");
    expect(output).toContain("KEYED_STATUS 200");
    expect(output).toContain("SENT getterRan=0 descriptor=absent");
    expect(output).toContain("APP_FETCH_STATUS 200");
    expect(output).toContain("APP_DISPATCHER_IS_BUNDLED_UNDICI false");
  });
  test("the built fi-core files load no undici module that reads the slot", () => {
    const script = `
      require(${JSON.stringify(INDEX)});
      const names = Object.keys(require.cache).filter(name => /undici/.test(name));
      console.log("UNDICI_MODULES " + names.length);
      console.log("GLOBAL_MODULE " + names.some(name => /undici[\\\\/]lib[\\\\/]global\\.js$/.test(name)));
      console.log("INDEX_MODULE " + names.some(name => /undici[\\\\/]index\\.js$/.test(name)));
    `;
    const output = spawnSync(process.execPath, ["-e", script], { encoding: "utf8", timeout: 30000, env: { PATH: process.env.PATH ?? "" } }).stdout;
    expect(output).toContain("GLOBAL_MODULE false");
    expect(output).toContain("INDEX_MODULE false");
  });
});

describe("one explanation of a refusal per process, whichever surface reports it first", () => {
  const KEY = "synthetic-fi-key-0001", SECRET = "synthetic-fi-secret-0001";
  const EXPLAIN = { http: /would go through an HTTP or HTTPS proxy/, grpc: /carries a Future AGI key header or a Future AGI key value/ };
  /** A real BatchSpanProcessor over the package's exporter, with a diag logger and a global error handler; three refused exports. */
  const BATCH = `
    const core = require(${JSON.stringify(INDEX)});
    const { BatchSpanProcessor, BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
    const { diag, DiagLogLevel } = require("@opentelemetry/api");
    const { setGlobalErrorHandler } = require("@opentelemetry/core");
    const lines = [];
    diag.setLogger({ verbose() {}, debug() {}, info() {}, warn: (...a) => lines.push("DIAG " + a.join(" ")), error: (...a) => lines.push("DIAG " + a.join(" ")) }, DiagLogLevel.WARN);
    setGlobalErrorHandler(error => lines.push("HANDLER " + (error && error.message)));
    const transport = process.env.TRANSPORT;
    const extra = transport === "grpc" ? { "x-other": ${JSON.stringify(KEY)} } : {};
    const exporter = core.createSpanExporter({ transport, endpoint: process.argv[1], timeoutMillis: 1500, headers: { ...extra, "x-api-key": ${JSON.stringify(KEY)}, "x-secret-key": ${JSON.stringify(SECRET)} } });
    const processor = new BatchSpanProcessor(exporter, { scheduledDelayMillis: 100000 });
    const provider = new BasicTracerProvider({ spanProcessors: [processor] });
    (async () => {
      for (let i = 0; i < 3; i++) { provider.getTracer("t").startSpan("s" + i).end(); try { await processor.forceFlush(); } catch (error) { lines.push("FLUSH " + (error && error.message)); } }
      for (const line of lines) console.log(line);
      process.exit(0);
    })();
  `;
  test.each([["http", { NODE_USE_ENV_PROXY: "1", HTTP_PROXY: "http://127.0.0.1:9" }], ["grpc", {}]] as const)("RED %s: over 3 refused exports with a real BatchSpanProcessor, a diag logger and a global error handler, exactly one line holds the explanation", async (transport, env) => {
    const target = await counter();
    const output = await runChild(BATCH, [transport === "http" ? `${target.url}/v1/traces` : `127.0.0.1:${target.port}`], { TRANSPORT: transport, ...env });
    const lines = output.split("\n").filter(line => /^(DIAG|HANDLER|FLUSH) /.test(line));
    expect(lines.filter(line => EXPLAIN[transport].test(line))).toHaveLength(1);
    // Every refused export still reports its code, and nothing else of the request.
    expect(lines.filter(line => /FI_(PLAINTEXT_PROXIED|AMBIENT_KEY_HEADERS)|PERMISSION_DENIED|not sent/.test(line)).length).toBeGreaterThanOrEqual(3);
    expect(output).not.toContain(KEY); expect(output).not.toContain(SECRET);
    expect(target.connections === 0 || transport === "grpc").toBe(true);
  });
});

describe("keyless https sends are not routed through the SDK proxy", () => {
  const cert = throwawayCertificate();
  afterAll(() => cert.dispose());
  test("a keyless OTLP export to an https collector goes direct while FI_HTTPS_PROXY names a proxy: the proxy sees 0 connections", async () => {
    const target = await sink({ tls: cert }), proxy = await counter();
    const script = `
      const core = require(${JSON.stringify(INDEX)});
      const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
      const exporter = core.createSpanExporter({ endpoint: process.argv[1], timeoutMillis: 4000 });
      const p = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(s) { exporter.export([s], r => { console.log("OTLP " + r.code); process.exit(0); }); }, forceFlush: async () => {}, shutdown: async () => {} }] });
      p.getTracer("t").startSpan("one").end();
    `;
    const output = await runChild(script, [`https://localhost:${target.port}/v1/traces`], { NODE_EXTRA_CA_CERTS: cert.caFile, FI_HTTPS_PROXY: proxy.url });
    expect(output).toContain("OTLP 0");
    expect(target.requests).toHaveLength(1);
    expect(proxy.connections).toBe(0); expect(proxy.bytes).toBe(0);
  });
});
