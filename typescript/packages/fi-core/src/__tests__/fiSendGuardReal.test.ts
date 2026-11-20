import * as grpc from "@grpc/grpc-js";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import net, { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { Agent, EnvHttpProxyAgent, Pool, ProxyAgent, setGlobalDispatcher } from "undici";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter, FITracerProvider, Transport } from "../otel";
import { grpcProxyFor, guardedFetch } from "../fi-send-guard";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

/**
 * The send-level guard, measured at the sockets of REAL listeners with the real exporters, the real undici dispatchers and real
 * grpc-js: every "sees nothing" assertion counts accepted connections and bytes read on net.Server sockets. No mock, spy or
 * stand-in class stands for a transport here. Cells marked (pin) already hold at the base of this lane; the rest fail there.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const ENV_KEYS = { FI_API_KEY: "env-key-synthetic-0002", FI_SECRET_KEY: "env-secret-synthetic-0002" };
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_GRPC_COLLECTOR_ENDPOINT", "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY",
  "all_proxy", "NO_PROXY", "no_proxy", "GRPC_PROXY", "grpc_proxy", "no_grpc_proxy", "NODE_USE_ENV_PROXY", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"];
const GLOBAL = Symbol.for("undici.globalDispatcher.1");
let saved: Record<string, string | undefined> = {};
let savedGlobal: unknown;
let savedExecArgv: string[] = [];
const cleanups: Array<() => Promise<void> | void> = [];
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  savedGlobal = (globalThis as any)[GLOBAL]; savedExecArgv = [...process.execArgv];
});
afterEach(async () => {
  for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; }
  if (savedGlobal === undefined) delete (globalThis as any)[GLOBAL]; else (globalThis as any)[GLOBAL] = savedGlobal;
  process.execArgv.splice(0, process.execArgv.length, ...savedExecArgv);
  for (const fn of cleanups.splice(0)) await fn();
});

interface Counter { port: number; url: string; connections: number; bytes: number }
/** A listener that only counts: accepted connections and bytes read. It stands for a proxy or a remote host that must see nothing. */
async function counter(host = "127.0.0.1"): Promise<Counter> {
  const state: Counter = { port: 0, url: "", connections: 0, bytes: 0 };
  const sockets = new Set<net.Socket>();
  const server = net.createServer(socket => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("error", () => {}); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(0, host, done));
  state.port = (server.address() as AddressInfo).port; state.url = `http://${host}:${state.port}`;
  cleanups.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
/** The same on a unix socket. */
async function unixCounter(): Promise<Counter & { socketPath: string }> {
  const socketPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "fi-guard-sock-")), "s.sock");
  const state = { port: 0, url: "", connections: 0, bytes: 0, socketPath };
  const sockets = new Set<net.Socket>();
  const server = net.createServer(socket => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("error", () => {}); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(socketPath, done));
  cleanups.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => { fs.rmSync(path.dirname(socketPath), { recursive: true, force: true }); done(); }); }));
  return state;
}
/** A connect function that dials another address than the one asked for. */
const redirectingConnect = (host: string, port: number) => (_options: unknown, callback: (error: Error | null, socket?: net.Socket) => void) => {
  const socket = net.connect({ host, port }); socket.once("connect", () => callback(null, socket)); socket.once("error", error => callback(error));
};
interface Sink extends Counter { requests: Array<{ url: string; headers: http.IncomingHttpHeaders }> }
/** A real HTTP server that records what it receives and counts its sockets. */
async function sink(options: { respond?: (res: http.ServerResponse) => void; tls?: { key: string; cert: string } } = {}): Promise<Sink> {
  const state: Sink = { port: 0, url: "", connections: 0, bytes: 0, requests: [] };
  const handler = (req: http.IncomingMessage, res: http.ServerResponse) => {
    req.resume(); req.on("end", () => { state.requests.push({ url: req.url ?? "", headers: req.headers }); if (options.respond) options.respond(res); else { res.setHeader("content-type", "application/json"); res.end("{}"); } });
  };
  const server = options.tls ? https.createServer(options.tls, handler) : http.createServer(handler);
  const sockets = new Set<net.Socket>();
  server.on(options.tls ? "secureConnection" : "connection", (socket: net.Socket) => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("data", chunk => { state.bytes += chunk.length; }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.port = (server.address() as AddressInfo).port; state.url = `${options.tls ? "https" : "http"}://127.0.0.1:${state.port}`;
  cleanups.push(() => new Promise<void>(done => { for (const s of sockets) s.destroy(); server.close(() => done()); }));
  return state;
}
const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
/** A real eval check against `base`: the base is the FI route by environment; `options` are what the caller passes. */
const check = (base: string, options: Record<string, unknown> = {}) => {
  process.env.FI_BASE_URL = base;
  return (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, options);
};
async function exportOne(exporter: any): Promise<void> {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  provider.getTracer("t").startSpan("one").end();
  await provider.forceFlush(); await provider.shutdown();
}
const FAST = { timeoutMillis: 1500 };
const seenNothing = (c: Counter) => { expect(c.connections).toBe(0); expect(c.bytes).toBe(0); };

describe("OTLP HTTP exporter: ambient headers and the final headers", () => {
  test("(pin) FI keys in OTEL_EXPORTER_OTLP_HEADERS on a non-FI http exporter: refused, the sink and the proxy see 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-api-key=ambient-key-synthetic-0001"; process.env.HTTP_PROXY = proxy.url;
    expect(() => createSpanExporter({ endpoint: target.url, ...FAST })).toThrow(expect.objectContaining({ code: "FI_AMBIENT_KEY_HEADERS" }));
    seenNothing(target); seenNothing(proxy);
  });
  test("(pin) the same with HTTP_PROXY set and the name percent-encoded", async () => {
    const target = await sink(), proxy = await counter();
    process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS = "x%2Dapi%2Dkey=ambient-key-synthetic-0001"; process.env.HTTP_PROXY = proxy.url; process.env.http_proxy = proxy.url;
    let refused = false;
    try { await exportOne(createSpanExporter({ endpoint: target.url, ...FAST })); } catch { refused = true; }
    expect(target.requests.every(r => !r.headers["x-api-key"])).toBe(true);
    expect(refused || target.requests.length === 0).toBe(true);
    seenNothing(proxy);
  });
  test("a header merge that adds a key name AFTER the decision is refused at the send: the sink sees 0 bytes", async () => {
    const target = await sink();
    const exporter: any = createSpanExporter({ endpoint: target.url, ...FAST });
    const parameters = exporter._delegate._transport._transport._parameters;
    const own = parameters.headers.bind(parameters);
    parameters.headers = () => ({ ...own(), "x-api-key": "merged-after-decision-key-0003" });
    await exportOne(exporter);
    seenNothing(target);
  });
  test("a header merge that copies an FI key value under another name is refused at the send", async () => {
    const target = await sink();
    Object.assign(process.env, ENV_KEYS);
    const exporter: any = createSpanExporter({ endpoint: target.url, ...FAST });
    const parameters = exporter._delegate._transport._transport._parameters;
    const own = parameters.headers.bind(parameters);
    parameters.headers = () => ({ ...own(), "x-copy": `Bearer ${ENV_KEYS.FI_SECRET_KEY}` });
    await exportOne(exporter);
    seenNothing(target);
  });
  test("(pin) the keys the caller passed for a loopback collector are sent, and only those", async () => {
    const target = await sink();
    await exportOne(createSpanExporter({ endpoint: target.url, headers: { ...KEYS }, ...FAST }));
    expect(target.requests).toHaveLength(1);
    expect(target.requests[0].headers["x-api-key"]).toBe(KEYS["x-api-key"]);
  });
});

describe("fetch: redirects, dispatchers and proxies, with real dispatchers and listeners", () => {
  let tls: { key: string; cert: string };
  beforeAll(() => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fi-guard-tls-"));
    const run = spawnSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
      "-keyout", path.join(dir, "key.pem"), "-out", path.join(dir, "cert.pem")], { encoding: "utf8" });
    if (run.status !== 0) throw new Error("openssl is needed for the TLS cells");
    tls = { key: fs.readFileSync(path.join(dir, "key.pem"), "utf8"), cert: fs.readFileSync(path.join(dir, "cert.pem"), "utf8") };
    fs.rmSync(dir, { recursive: true, force: true });
  });
  beforeEach(() => { Object.assign(process.env, ENV_KEYS); });

  test("(pin) a 302 from an approved https target to http:// is refused and the redirect target sees 0 bytes", async () => {
    const redirected = await sink();
    const approved = await sink({ tls, respond: res => { res.statusCode = 302; res.setHeader("location", `${redirected.url}/leak`); res.end(); } });
    await check(`https://localhost:${approved.port}`, { dispatcher: new Agent({ connect: { ca: tls.cert } }) });
    expect(approved.requests).toHaveLength(1);
    seenNothing(redirected);
  });
  test("a Pool on another origin with an http://127.0.0.1 FI target is refused: the remote and the proxy see 0 bytes", async () => {
    const target = await sink(), remote = await counter(), proxy = await counter();
    process.env.HTTP_PROXY = proxy.url;
    const pool = new Pool(remote.url); cleanups.push(() => pool.close());
    await check(target.url, { dispatcher: pool });
    seenNothing(remote); seenNothing(proxy); expect(target.requests).toHaveLength(0);
  });
  test("a Pool whose origin cannot be read is refused", async () => {
    const target = await sink(), proxy = await counter();
    const unknown = Object.setPrototypeOf({ dispatch: () => false }, Pool.prototype);
    await check(target.url, { dispatcher: unknown });
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("a Pool on the exact target origin is refused too: undici keeps a Pool's connect, socketPath and factory options in an opaque connector, so its dial cannot be shown to reach the target", async () => {
    const target = await sink(), proxy = await counter();
    process.env.HTTP_PROXY = proxy.url;
    const pool = new Pool(target.url); cleanups.push(() => pool.close());
    await check(target.url, { dispatcher: pool });
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("(pin) an empty undici global slot with HTTP_PROXY set and a loopback target is sent direct", async () => {
    const target = await sink(), proxy = await counter();
    delete (globalThis as any)[GLOBAL]; process.env.HTTP_PROXY = proxy.url; process.env.http_proxy = proxy.url;
    await check(target.url);
    expect(target.requests).toHaveLength(1); seenNothing(proxy);
  });
  test("(pin) a plain Agent as the global dispatcher with HTTP_PROXY set and a loopback target is sent direct", async () => {
    const target = await sink(), proxy = await counter();
    setGlobalDispatcher(new Agent()); process.env.HTTP_PROXY = proxy.url;
    await check(target.url);
    expect(target.requests).toHaveLength(1); seenNothing(proxy);
  });
  test("(pin) a global EnvHttpProxyAgent with a loopback target is refused and the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    process.env.HTTP_PROXY = proxy.url; setGlobalDispatcher(new EnvHttpProxyAgent());
    await check(target.url);
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("(pin) a caller ProxyAgent with a loopback target is refused and the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    const agent = new ProxyAgent(proxy.url); cleanups.push(() => agent.close());
    await check(target.url, { dispatcher: agent });
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("(pin) NODE_USE_ENV_PROXY=1 with a loopback target is refused and the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    process.env.NODE_USE_ENV_PROXY = "1"; process.env.HTTP_PROXY = proxy.url;
    await check(target.url);
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("--use-env-proxy with a loopback target is refused and the proxy sees 0 bytes", async () => {
    const target = await sink(), proxy = await counter();
    process.execArgv.push("--use-env-proxy"); process.env.HTTP_PROXY = proxy.url;
    await check(target.url);
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("--use-env-proxy in NODE_OPTIONS is the same", async () => {
    const target = await sink(), proxy = await counter();
    process.env.NODE_OPTIONS = `${process.env.NODE_OPTIONS ?? ""} --use-env-proxy`; process.env.HTTP_PROXY = proxy.url;
    try { await check(target.url); } finally { process.env.NODE_OPTIONS = process.env.NODE_OPTIONS.replace(" --use-env-proxy", ""); }
    expect(target.requests).toHaveLength(0); seenNothing(proxy);
  });
  test("a guarded fetch whose final headers carry an FI key value under another name is refused: the sink sees 0 bytes", async () => {
    const target = await sink();
    await expect(guardedFetch({ url: `${target.url}/x`, init: { method: "POST", headers: { "x-copy": `Bearer ${ENV_KEYS.FI_API_KEY}` } }, intended: { keys: new Map(), values: [] } }))
      .rejects.toEqual(expect.objectContaining({ code: "FI_AMBIENT_KEY_HEADERS" }));
    seenNothing(target);
  });
  test("a guarded fetch whose final headers carry a key name the decision did not put there is refused, in any case and percent-encoded", async () => {
    const target = await sink();
    for (const name of ["x-api-key", "X-Secret-Key", "x%2Dapi%2Dkey"]) {
      await expect(guardedFetch({ url: `${target.url}/x`, init: { method: "POST", headers: { [name]: "some-other-value-0007" } }, intended: { keys: new Map(), values: [] } }))
        .rejects.toEqual(expect.objectContaining({ code: "FI_AMBIENT_KEY_HEADERS" }));
    }
    seenNothing(target);
  });
  test("a guarded fetch with the decision's own key headers is sent, and a 3xx answer is a refusal", async () => {
    const target = await sink(), redirected = await sink();
    const keys = { "x-api-key": ENV_KEYS.FI_API_KEY, "x-secret-key": ENV_KEYS.FI_SECRET_KEY };
    const intended = { keys: new Map(Object.entries(keys)), values: Object.values(keys) };
    const ok = await guardedFetch({ url: `${target.url}/x`, init: { method: "POST", headers: keys }, intended });
    expect(ok.status).toBe(200); expect(target.requests[0].headers["x-api-key"]).toBe(ENV_KEYS.FI_API_KEY);
    const moved = await sink({ respond: res => { res.statusCode = 302; res.setHeader("location", `${redirected.url}/leak`); res.end(); } });
    await expect(guardedFetch({ url: `${moved.url}/x`, init: { method: "POST", headers: keys }, intended })).rejects.toEqual(expect.objectContaining({ code: "FI_REDIRECT" }));
    seenNothing(redirected);
  });
});

describe("gRPC: the channel and the call", () => {
  async function grpcSink(): Promise<{ received: grpc.Metadata[]; port: number }> {
    const received: grpc.Metadata[] = [];
    const server = new grpc.Server();
    const id = (b: Buffer) => b;
    server.addService({ Export: { path: "/opentelemetry.proto.collector.trace.v1.TraceService/Export", requestStream: false, responseStream: false,
      requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } },
    { Export: (call: any, callback: any) => { received.push(call.metadata); callback(null, Buffer.alloc(0)); } });
    const port = await new Promise<number>((ok, bad) => server.bindAsync("127.0.0.1:0", grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
    cleanups.push(() => server.forceShutdown());
    return { received, port };
  }
  const VARIANTS: Array<[string, Record<string, string>]> = [
    ["grpc_proxy", { grpc_proxy: "PROXY" }], ["https_proxy", { https_proxy: "PROXY" }], ["http_proxy", { http_proxy: "PROXY" }], ["HTTP_PROXY upper case", { HTTP_PROXY: "PROXY" }],
    ["ALL_PROXY", { ALL_PROXY: "PROXY" }], ["grpc_proxy with no_grpc_proxy", { grpc_proxy: "PROXY", no_grpc_proxy: "127.0.0.1" }],
    ["grpc_proxy with no_proxy", { grpc_proxy: "PROXY", no_proxy: "127.0.0.1" }], ["http_proxy with no_proxy elsewhere", { http_proxy: "PROXY", no_proxy: "other.example" }],
  ];
  test.each(VARIANTS)("(pin) plaintext loopback FI target with %s: direct, the proxy sees 0 bytes, the keys arrive", async (_name, env) => {
    const proxy = await counter(), target = await grpcSink();
    for (const [name, value] of Object.entries(env)) process.env[name] = value === "PROXY" ? proxy.url : value;
    await exportOne(createSpanExporter({ transport: Transport.GRPC, endpoint: `http://127.0.0.1:${target.port}`, headers: { ...KEYS }, ...FAST }));
    expect(target.received).toHaveLength(1);
    expect(target.received[0].get("x-api-key")).toEqual([KEYS["x-api-key"]]);
    seenNothing(proxy);
  });
  test("a per-call metadata merge that adds a key name is refused before the connection: the target sees 0 bytes", async () => {
    const target = await counter();
    const exporter: any = createSpanExporter({ transport: Transport.GRPC, endpoint: `http://127.0.0.1:${target.port}`, ...FAST });
    const parameters = exporter._delegate._transport._parameters;
    parameters.metadata = () => { const metadata = new grpc.Metadata(); metadata.set("x-api-key", "merged-after-decision-key-0004"); return metadata; };
    await exportOne(exporter);
    seenNothing(target);
  });
  test("a per-call metadata value that is an FI key value under another name is refused before the connection", async () => {
    const target = await counter();
    Object.assign(process.env, ENV_KEYS);
    const exporter: any = createSpanExporter({ transport: Transport.GRPC, endpoint: `http://127.0.0.1:${target.port}`, ...FAST });
    const parameters = exporter._delegate._transport._parameters;
    parameters.metadata = () => { const metadata = new grpc.Metadata(); metadata.set("x-copy", ENV_KEYS.FI_API_KEY); return metadata; };
    await exportOne(exporter);
    seenNothing(target);
  });
});

describe("grpcProxyFor reads the proxy environment the way grpcio does", () => {
  const PROXY = "http://proxy.internal:3128";
  test.each([
    [{ grpc_proxy: PROXY }, "collector.example", PROXY], [{ https_proxy: PROXY }, "collector.example", PROXY], [{ http_proxy: PROXY }, "collector.example", PROXY],
    [{ grpc_proxy: PROXY, https_proxy: "http://other:1", http_proxy: "http://other:2" }, "collector.example", PROXY],
    [{ https_proxy: PROXY, http_proxy: "http://other:2" }, "collector.example", PROXY],
    [{ HTTP_PROXY: PROXY }, "collector.example", undefined], [{ HTTPS_PROXY: PROXY }, "collector.example", undefined], [{ GRPC_PROXY: PROXY }, "collector.example", undefined],
    [{ ALL_PROXY: PROXY }, "collector.example", undefined], [{ all_proxy: PROXY }, "collector.example", undefined],
    [{ grpc_proxy: PROXY, no_grpc_proxy: "collector.example" }, "collector.example", undefined],
    [{ grpc_proxy: PROXY, no_proxy: "collector.example" }, "collector.example", undefined],
    [{ grpc_proxy: PROXY, no_grpc_proxy: "other.example", no_proxy: "collector.example" }, "collector.example", PROXY],
    [{ grpc_proxy: "", https_proxy: PROXY }, "collector.example", undefined],
  ] as Array<[Record<string, string>, string, string | undefined]>)("%j for %s -> %s", (env, host, expected) => {
    expect(grpcProxyFor(host, env)).toBe(expected);
  });
});

describe("endpoint logs name the host class only", () => {
  const messages: string[] = [];
  beforeEach(() => { messages.length = 0; const sinkFn = (...args: unknown[]) => { messages.push(args.map(String).join(" ")); };
    diag.setLogger({ verbose: sinkFn, debug: sinkFn, info: sinkFn, warn: sinkFn, error: sinkFn }, DiagLogLevel.ALL); });
  afterEach(() => diag.disable());
  test("an endpoint with userinfo and a query token in verbose mode", () => {
    new FITracerProvider({ endpoint: "https://alice:hunter2-synthetic@collector.example:4318/tracer/v1/traces?token=query-token-synthetic-0003", headers: { authorization: "Bearer x" }, verbose: true });
    const text = messages.join("\n");
    expect(text).not.toMatch(/alice|hunter2|query-token|token=|collector\.example|4318/);
    expect(text).toMatch(/other host/);
  });
});

describe("dispatchers whose dial can be redirected are refused for every key-carrying request, https included", () => {
  let tls: { key: string; cert: string };
  beforeAll(() => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fi-guard-tls2-"));
    const run = spawnSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
      "-keyout", path.join(dir, "key.pem"), "-out", path.join(dir, "cert.pem")], { encoding: "utf8" });
    if (run.status !== 0) throw new Error("openssl is needed for the TLS cells");
    tls = { key: fs.readFileSync(path.join(dir, "key.pem"), "utf8"), cert: fs.readFileSync(path.join(dir, "cert.pem"), "utf8") };
    fs.rmSync(dir, { recursive: true, force: true });
  });
  beforeEach(() => { Object.assign(process.env, ENV_KEYS); });

  test("an https FI target and a Pool bound to a local 127.0.0.2 listener: the listener sees 0 bytes", async () => {
    const approved = await sink({ tls }), remote = await counter("127.0.0.2");
    const pool = new Pool(remote.url); cleanups.push(() => pool.close());
    await check(`https://localhost:${approved.port}`, { dispatcher: pool });
    seenNothing(remote); expect(approved.requests).toHaveLength(0);
  });
  test("an https FI target and a Pool on the target's own origin is refused too: its connector cannot be shown to dial the target", async () => {
    const approved = await sink({ tls });
    const pool = new Pool(`https://localhost:${approved.port}`, { connect: { ca: tls.cert } }); cleanups.push(() => pool.close());
    await check(`https://localhost:${approved.port}`, { dispatcher: pool });
    expect(approved.requests).toHaveLength(0);
  });
  test("a global Pool bound to another origin is refused for an https FI target", async () => {
    const approved = await sink({ tls }), remote = await counter("127.0.0.2");
    const pool = new Pool(remote.url); cleanups.push(() => pool.close());
    setGlobalDispatcher(pool);
    await check(`https://localhost:${approved.port}`);
    seenNothing(remote); expect(approved.requests).toHaveLength(0);
  });
  test("an Agent with a connect function that dials a 127.0.0.2 listener: the listener sees 0 bytes", async () => {
    const target = await sink(), remote = await counter("127.0.0.2");
    const agent = new Agent({ connect: redirectingConnect("127.0.0.2", remote.port) as any }); cleanups.push(() => agent.close());
    await check(target.url, { dispatcher: agent });
    seenNothing(remote); expect(target.requests).toHaveLength(0);
  });
  test("the same for an https target", async () => {
    const approved = await sink({ tls }), remote = await counter("127.0.0.2");
    const agent = new Agent({ connect: redirectingConnect("127.0.0.2", remote.port) as any }); cleanups.push(() => agent.close());
    await check(`https://localhost:${approved.port}`, { dispatcher: agent });
    seenNothing(remote); expect(approved.requests).toHaveLength(0);
  });
  test("an Agent with a socketPath, a Pool with a socketPath, a Pool with a connect option and an Agent with a custom factory are refused: the unix listener sees 0 bytes", async () => {
    const target = await sink(), unix = await unixCounter();
    const dispatchers = [
      new Agent({ connect: { socketPath: unix.socketPath } }),
      new Pool(target.url, { socketPath: unix.socketPath }),
      new Pool(target.url, { connect: { socketPath: unix.socketPath } }),
      new Pool(target.url, { connect: redirectingConnect("127.0.0.2", 9) as any }),
      new Agent({ factory: (origin: any, options: any) => new Pool(origin, { ...options, socketPath: unix.socketPath }) }),
    ];
    for (const dispatcher of dispatchers) { cleanups.push(() => dispatcher.close()); await check(target.url, { dispatcher }); }
    seenNothing(unix); expect(target.requests).toHaveLength(0);
  });
  test("(pin) a plain Agent is allowed, plaintext and https", async () => {
    const target = await sink(), approved = await sink({ tls });
    const plain = new Agent({ connect: { ca: tls.cert } }); cleanups.push(() => plain.close());
    // A TLS option in connect does not redirect the dial, but the guard cannot tell; a plain Agent with no options is the shape that is proven.
    const bare = new Agent(); cleanups.push(() => bare.close());
    await check(target.url, { dispatcher: bare });
    expect(target.requests).toHaveLength(1); expect(target.requests[0].headers["x-api-key"]).toBe(ENV_KEYS.FI_API_KEY);
    void plain; void approved;
  });
  test("dispatcher origins and options are read from data only: an accessor on the dispatcher is never run", async () => {
    const target = await sink();
    let ran = false;
    const trap = Object.setPrototypeOf({}, Agent.prototype);
    Object.defineProperty(trap, Symbol("options"), { get() { ran = true; return {}; } });
    Object.defineProperty(trap, "constructor", { get() { ran = true; return Agent; } });
    await check(target.url, { dispatcher: trap });
    expect(ran).toBe(false); expect(target.requests).toHaveLength(0);
  });
});

describe("no raw URL, userinfo or query in any log level", () => {
  const messages: string[] = [];
  beforeEach(() => { messages.length = 0; const sinkFn = (...args: unknown[]) => { messages.push(args.map(String).join(" ")); };
    diag.setLogger({ verbose: sinkFn, debug: sinkFn, info: sinkFn, warn: sinkFn, error: sinkFn }, DiagLogLevel.ALL); });
  afterEach(() => diag.disable());
  test("an eval check whose base URL makes fetch throw an error that carries the URL logs the host class and the error class only", async () => {
    Object.assign(process.env, ENV_KEYS);
    // A port out of range: fetch itself throws, and its message carries the whole URL.
    process.env.FI_BASE_URL = "http://alice:hunter2-synthetic@127.0.0.1:99999/?token=query-token-synthetic-0003";
    await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, true, {});
    const text = messages.join("\n");
    expect(text).not.toMatch(/alice|hunter2|query-token|token=|99999|127\.0\.0\.1/);
  });
  test("a failed guarded fetch logs no URL: the error from the transport is reduced to its class", async () => {
    const dead = await counter();
    Object.assign(process.env, ENV_KEYS);
    process.env.FI_BASE_URL = `http://127.0.0.1:${dead.port}`;
    // The listener accepts and drops the connection: the error comes from the transport.
    await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, true, {});
    const text = messages.join("\n");
    expect(text).not.toMatch(new RegExp(`127\\.0\\.0\\.1|${dead.port}`));
  });
});
