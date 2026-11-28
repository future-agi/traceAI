import dns from "node:dns";
import net from "node:net";
import { ExportResultCode } from "@opentelemetry/core";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { Agent, EnvHttpProxyAgent, ProxyAgent, setGlobalDispatcher } from "undici";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter } from "../otel";
import { guardedFetch } from "../fi-send-guard";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";
import { BUILT_GUARD, cleanups, connectProxy, counter, redirectingConnect, runChild, runCleanups, sink, throwawayCertificate, UNDICI } from "./helpers/realSockets";

/**
 * A key-carrying send uses fi-core's own transport only. A caller's `dispatcher` option is refused with FI_DISPATCHER_UNKNOWN, whatever the
 * dispatcher is called or looks like, plaintext and https alike, at every send; the undici global slot is never read, so what an application
 * installs there neither refuses nor routes a send. Real sockets throughout: the accepted connections and bytes of the proxy, the remote and the
 * sink are counted on net.Server sockets; nothing is mocked.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const ENV_KEYS = { FI_API_KEY: "env-key-synthetic-0002", FI_SECRET_KEY: "env-secret-synthetic-0002" };
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NODE_USE_ENV_PROXY"];
const SLOT = Symbol.for("undici.globalDispatcher.1");
let saved: Record<string, string | undefined> = {};
let savedSlot: PropertyDescriptor | undefined;
beforeEach(() => { saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; } savedSlot = Object.getOwnPropertyDescriptor(globalThis, SLOT); Object.assign(process.env, ENV_KEYS); });
afterEach(async () => {
  for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; }
  // Put the slot back exactly as it was: the accessor and Proxy cells replace it, and a data property can only be redefined while configurable.
  const current = Object.getOwnPropertyDescriptor(globalThis, SLOT);
  if (current !== savedSlot) { if (current?.configurable) delete (globalThis as any)[SLOT]; if (savedSlot) Object.defineProperty(globalThis, SLOT, savedSlot); }
  await runCleanups();
});
const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
const check = (base: string, options: Record<string, unknown> = {}) => { process.env.FI_BASE_URL = base; return (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, options); };
const seenNothing = (c: { connections: number; bytes: number }) => { expect(c.connections).toBe(0); expect(c.bytes).toBe(0); };
const intended = { keys: new Map(Object.entries({ "x-api-key": ENV_KEYS.FI_API_KEY, "x-secret-key": ENV_KEYS.FI_SECRET_KEY })), values: [ENV_KEYS.FI_API_KEY, ENV_KEYS.FI_SECRET_KEY] };
const keyHeaders = { "x-api-key": ENV_KEYS.FI_API_KEY, "x-secret-key": ENV_KEYS.FI_SECRET_KEY };
async function exportOne(exporter: any): Promise<number> {
  return new Promise<number>(done => {
    const provider = new BasicTracerProvider({ spanProcessors: [{ onStart() {}, onEnd(span: any) { exporter.export([span], (result: { code: number }) => done(result.code)); }, forceFlush: async () => {}, shutdown: async () => {} } as any] });
    provider.getTracer("t").startSpan("one").end();
  });
}

/** A child process of the built package: the application and Node share one global object there, so the undici slot is the real one. */
const CHILD = `
  const net = require("node:net");
  const slot = Symbol.for("undici.globalDispatcher.1");
  const keys = { "x-api-key": "env-key-synthetic-0002", "x-secret-key": "env-secret-synthetic-0002" };
  const intended = { keys: new Map(Object.entries(keys)), values: Object.values(keys) };
  const scenario = process.env.SCENARIO;
  const undici = () => require(${JSON.stringify(UNDICI)});
  if (scenario === "accessor-slot") Object.defineProperty(globalThis, slot, { configurable: true, get() { console.log("GETTER_RAN"); return undefined; } });
  const guard = require(${JSON.stringify(BUILT_GUARD)});
  const dist = ${JSON.stringify(BUILT_GUARD.replace(/fi-send-guard$/, "index"))};
  async function send(url) {
    try { const r = await guard.guardedFetch({ url, init: { method: "POST", headers: keys }, intended }); console.log("STATUS " + r.status); }
    catch (e) { console.log("REFUSED " + e.code); }
  }
  (async () => {
    const url = process.argv[1];
    if (scenario === "envproxy-connect") {
      const { EnvHttpProxyAgent, setGlobalDispatcher } = undici();
      setGlobalDispatcher(new EnvHttpProxyAgent({ connect: (options, callback) => { const socket = net.connect({ host: "127.0.0.2", port: Number(process.env.REMOTE_PORT) }); socket.once("connect", () => callback(null, socket)); socket.once("error", callback); } }));
      await send(url);
    } else if (scenario === "proxy-wrapped-agent") { const { Agent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new Proxy(new Agent(), {})); await send(url); }
    else if (scenario === "plain-agent") { const { Agent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new Agent()); await send(url); }
    else if (scenario === "proxy-agent-https") { const { ProxyAgent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new ProxyAgent(process.env.PROXY_URL)); await send(url); }
    else if (scenario === "accessor-slot") await send(url);
    else if (scenario === "replaced-after-init") {
      await send(url);
      const { ProxyAgent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new ProxyAgent(process.env.PROXY_URL));
      await send(url);
    } else if (scenario === "otlp-replaced-after-init") {
      const { diag, DiagLogLevel } = require("@opentelemetry/api");
      const log = (...a) => console.log(a.join(" ")); diag.setLogger({ verbose() {}, debug() {}, info() {}, warn: log, error: log }, DiagLogLevel.WARN);
      const { createSpanExporter } = require(dist);
      const exporter = createSpanExporter({ endpoint: url, headers: { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" }, timeoutMillis: 1500 });
      const { ProxyAgent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new ProxyAgent(process.env.PROXY_URL));
      const span = { name: "x", kind: 0, spanContext: () => ({ traceId: "0".repeat(31) + "1", spanId: "0".repeat(15) + "1", traceFlags: 1 }), startTime: [0, 0], endTime: [0, 1], status: { code: 0 }, attributes: {}, links: [], events: [],
        duration: [0, 1], ended: true, resource: { attributes: {} }, instrumentationLibrary: { name: "t" }, droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0 };
      await new Promise(done => exporter.export([span], result => { console.log(result.code === 1 ? "EXPORT FAILED" : "EXPORT OK"); done(); }));
      await exporter.shutdown();
    } else if (scenario === "eval-replaced-after-init") {
      process.env.FI_API_KEY = keys["x-api-key"]; process.env.FI_SECRET_KEY = keys["x-secret-key"]; process.env.FI_BASE_URL = url;
      const { checkCustomEvalConfigExists } = require(dist);
      const tag = { type: "observation_span", value: "llm", eval_name: "Chunk_Attribution", config: {}, custom_eval_name: "Chunk_Attribution", mapping: {}, model: "turing_small", toDict: () => ({ eval_name: "Chunk_Attribution", custom_eval_name: "Chunk_Attribution" }) };
      await checkCustomEvalConfigExists("p", [tag], undefined, false, {});
      const { ProxyAgent, setGlobalDispatcher } = undici(); setGlobalDispatcher(new ProxyAgent(process.env.PROXY_URL));
      await checkCustomEvalConfigExists("p", [tag], undefined, false, {});
      console.log("EVAL-DONE");
    }
  })();
`;

describe("a dispatcher is never trusted by name or shape", () => {
  const cert = throwawayCertificate(); afterAll(() => cert.dispose());
  test("an https FI target with a REAL undici ProxyAgent pointing at a local proxy is refused: the proxy sees 0 bytes", async () => {
    const approved = await sink({ tls: cert }), proxy = await counter();
    const agent = new ProxyAgent(proxy.url); cleanups.push(() => agent.close());
    await check(`https://localhost:${approved.port}`, { dispatcher: agent });
    seenNothing(proxy); expect(approved.requests).toHaveLength(0);
  });
  test("an EnvHttpProxyAgent built with connect pointing at a 127.0.0.2 listener, installed globally, is not read: the send reaches the real target, the listener sees 0 bytes", async () => {
    const approved = await sink({ tls: cert }), remote = await counter("127.0.0.2");
    const output = await runChild(CHILD, [`https://localhost:${approved.port}/x`], { SCENARIO: "envproxy-connect", REMOTE_PORT: String(remote.port), NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(output).toContain("STATUS 200");
    seenNothing(remote); expect(approved.requests).toHaveLength(1);
  });
  test("an empty class named ProxyAgent as a caller dispatcher is refused with FI_DISPATCHER_UNKNOWN, https and plaintext", async () => {
    class ProxyAgentLookalike {}
    Object.defineProperty(ProxyAgentLookalike, "name", { value: "ProxyAgent" });
    for (const url of ["https://localhost:9/x", "http://127.0.0.1:9/x"]) {
      await expect(guardedFetch({ url, init: { method: "POST", headers: keyHeaders, dispatcher: new ProxyAgentLookalike() as any }, intended })).rejects.toEqual(expect.objectContaining({ code: "FI_DISPATCHER_UNKNOWN" }));
    }
  });
  test.each(["proxy-wrapped-agent", "plain-agent", "accessor-slot", "proxy-agent-https"])("a global dispatcher installed by the application is not read: %s", async scenario => {
    const target = await sink({ tls: scenario === "proxy-agent-https" ? cert : undefined }), proxy = await counter();
    const url = scenario === "proxy-agent-https" ? `https://localhost:${target.port}/x` : `${target.url}/x`;
    const output = await runChild(CHILD, [url], { SCENARIO: scenario, PROXY_URL: proxy.url, NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(output).toContain("STATUS 200");
    // a getter the application put on the slot before load is never invoked by loading fi-core or by a keyed send
    expect(output).not.toContain("GETTER_RAN");
    seenNothing(proxy); expect(target.requests).toHaveLength(1);
  });
  test("a caller's plain Agent as the dispatcher option is refused: the option is not accepted for a key-carrying fetch", async () => {
    const target = await sink();
    const agent = new Agent(); cleanups.push(() => agent.close());
    await check(target.url, { dispatcher: agent });
    seenNothing(target);
  });
});

describe("a dispatcher installed AFTER initialisation is not read at the send", () => {
  test("a guarded fetch: setGlobalDispatcher(new ProxyAgent(...)) after the package loaded changes nothing, and the proxy and remote see 0 bytes", async () => {
    const target = await sink(), proxy = await counter(), remote = await counter("127.0.0.2");
    const output = await runChild(CHILD, [`${target.url}/x`], { SCENARIO: "replaced-after-init", PROXY_URL: proxy.url });
    expect((output.match(/STATUS 200/g) ?? []).length).toBe(2);
    expect(target.requests).toHaveLength(2); seenNothing(proxy); seenNothing(remote);
  });
  test("an OTLP HTTP export: an exporter built before the slot is replaced still exports, and nothing reaches the proxy", async () => {
    const target = await sink(), proxy = await counter(), remote = await counter("127.0.0.2");
    const output = await runChild(CHILD, [target.url], { SCENARIO: "otlp-replaced-after-init", PROXY_URL: proxy.url });
    expect(output).toContain("EXPORT OK");
    expect(target.requests).toHaveLength(1); seenNothing(proxy); seenNothing(remote);
  });
  test("an eval check through the public function: the same replacement changes nothing", async () => {
    const target = await sink(), proxy = await counter();
    const output = await runChild(CHILD, [target.url], { SCENARIO: "eval-replaced-after-init", PROXY_URL: proxy.url });
    expect(output).toContain("EVAL-DONE");
    expect(target.requests).toHaveLength(2); seenNothing(proxy);
  });
});

describe("a lone key header counts as key-carrying", () => {
  test.each(["x-api-key", "X-Secret-Key", "x%2Dapi%2Dkey"])("a lone %s carrying the FI key to a non-FI http host is refused under the plaintext rules", async name => {
    const target = await sink();
    const original = dns.lookup;
    (dns as any).lookup = (_host: string, options: any, callback?: any) => { const done = typeof options === "function" ? options : callback; process.nextTick(() => (options && options.all) ? done(null, [{ address: "127.0.0.1", family: 4 }]) : done(null, "127.0.0.1", 4)); };
    try {
      // 127.1 is not the literal loopback: plaintext to it needs the opt-in, and it resolves to this machine, so a send would reach the sink.
      await expect(guardedFetch({ url: `http://127.1:${target.port}/x`, init: { method: "POST", headers: { [name]: ENV_KEYS.FI_API_KEY } },
        intended: { keys: new Map([[decodeURIComponent(name).toLowerCase(), ENV_KEYS.FI_API_KEY]]), values: [ENV_KEYS.FI_API_KEY] } })).rejects.toEqual(expect.objectContaining({ code: "FI_PLAINTEXT_NOT_ALLOWED" }));
    } finally { (dns as any).lookup = original; }
    seenNothing(target);
  });
  test("a lone key header on a gRPC-bound exporter metadata is judged the same way", async () => {
    const target = await counter();
    const exporter: any = createSpanExporter({ transport: "grpc" as any, endpoint: `http://127.0.0.1:${target.port}`, timeoutMillis: 800 } as any);
    const parameters = exporter._delegate._transport._parameters;
    parameters.metadata = () => { const grpc = require("@grpc/grpc-js"); const metadata = new grpc.Metadata(); metadata.set("x-secret-key", ENV_KEYS.FI_SECRET_KEY); return metadata; };
    await exportOne(exporter);
    seenNothing(target);
  });
});

describe("controls: the transports that are accepted", () => {
  const cert = throwawayCertificate(); afterAll(() => cert.dispose());
  const SCRIPT = `const { guardedFetch } = require(${JSON.stringify(BUILT_GUARD)});
    const keys = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
    const intended = { keys: new Map(Object.entries(keys)), values: Object.values(keys) };
    (async () => {
      if (process.env.APP_FETCH_FIRST) await fetch(process.env.APP_FETCH_FIRST).then(r => r.text()).catch(() => {});
      if (process.env.APP_SET_DISPATCHER_FIRST) { const { Agent, setGlobalDispatcher } = require(${JSON.stringify(UNDICI)}); setGlobalDispatcher(new Agent()); }
      const { guardedFetch: late } = process.env.LOAD_AFTER_DISPATCHER ? require(${JSON.stringify(BUILT_GUARD)}) : { guardedFetch };
      try { const r = await late({ url: process.argv[1], init: { method: "POST", headers: keys }, intended }); console.log("STATUS " + r.status); }
      catch (e) { console.log("REFUSED " + e.code); }
    })();`;
  test("CONTROL: an empty slot and an https target to a local TLS sink with a test CA is sent direct, TLS verified, received", async () => {
    const approved = await sink({ tls: cert });
    const output = await runChild(SCRIPT, [`https://localhost:${approved.port}/x`], { NODE_EXTRA_CA_CERTS: cert.caFile });
    expect(output).toContain("STATUS 200");
    expect(approved.requests).toHaveLength(1); expect(approved.requests[0].headers["x-api-key"]).toBe("synthetic-fi-key-0001");
  });
  test("CONTROL: an application fetch before the first guarded send leaves Node's own default in the slot, and it is accepted", async () => {
    const plain = await sink();
    const output = await runChild(SCRIPT, [`${plain.url}/x`], { APP_FETCH_FIRST: `${plain.url}/warm` });
    expect(output).toContain("STATUS 200");
  });
  test("NODE_USE_ENV_PROXY=1 with HTTPS_PROXY: an https send tunnels through the proxy with fi-core's own proxy agent: the proxy sees CONNECT only, the target gets the keys", async () => {
    const approved = await sink({ tls: cert });
    const proxy = await connectProxy(approved.port);
    const output = await runChild(SCRIPT, [`https://localhost:${approved.port}/x`], { NODE_EXTRA_CA_CERTS: cert.caFile, NODE_USE_ENV_PROXY: "1", HTTPS_PROXY: proxy.url, NO_PROXY: "" });
    expect(output).toContain("STATUS 200");
    expect(proxy.connects).toEqual([`localhost:${approved.port}`]); expect(approved.requests).toHaveLength(1);
    expect(proxy.seen).not.toContain("synthetic-fi-key-0001");
  });
  test("CONTROL: NODE_USE_ENV_PROXY=1 with no proxy variable leaves the slot empty at load: an https target is sent direct and TLS is verified", async () => {
    const approved = await sink({ tls: cert });
    const output = await runChild(SCRIPT, [`https://localhost:${approved.port}/x`], { NODE_EXTRA_CA_CERTS: cert.caFile, NODE_USE_ENV_PROXY: "1" });
    expect(output).toContain("STATUS 200");
    expect(approved.requests).toHaveLength(1);
  });
  test("NODE_USE_ENV_PROXY=1 with a proxy variable and a plaintext target is refused: the proxy and the target see nothing", async () => {
    const plain = await sink(), proxy = await counter();
    const output = await runChild(SCRIPT, [`${plain.url}/x`], { NODE_USE_ENV_PROXY: "1", HTTP_PROXY: proxy.url });
    expect(output).toContain("REFUSED FI_PLAINTEXT_PROXIED");
    seenNothing(proxy); expect(plain.requests).toHaveLength(0);
  });
  test("CONTROL: a dispatcher the application installed BEFORE the package was loaded is not read: the send succeeds", async () => {
    const plain = await sink();
    const output = await runChild(SCRIPT.replace(`const { guardedFetch } = require(${JSON.stringify(BUILT_GUARD)});`, "let guardedFetch;"), [`${plain.url}/x`], { APP_SET_DISPATCHER_FIRST: "1", LOAD_AFTER_DISPATCHER: "1" });
    expect(output).toContain("STATUS 200");
    expect(plain.requests).toHaveLength(1);
  });
});
void net;
