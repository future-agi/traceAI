import * as grpc from "@grpc/grpc-js";
import http from "node:http";
import net from "node:net";
import { AddressInfo } from "node:net";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter, HTTPSpanExporter, Transport } from "../otel";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";
import { Agent } from "undici";

/**
 * A plaintext request that carries Future AGI keys under the opt-in never goes through a proxy.
 *   - A request the SDK builds itself is built with proxies off: HTTP with its own non-proxy agent, gRPC with the channel
 *     option grpc.enable_http_proxy: 0, a fetch with a global dispatcher that is not a proxy. No environment variable can
 *     route it.
 *   - Where the caller supplies the dispatcher (or sets a global one), the request counts as proxied when EITHER the
 *     sending library's own resolution OR Python's proxy_bypass_environment says so, and an uninspectable dispatcher
 *     counts as proxied: refused, with no key on the wire.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
  "NO_PROXY", "no_proxy", "GRPC_PROXY", "grpc_proxy", "no_grpc_proxy", "NODE_USE_ENV_PROXY"];
const GLOBAL = Symbol.for("undici.globalDispatcher.1");
let saved: Record<string, string | undefined> = {};
let savedGlobal: unknown;
let fetchSpy: jest.SpyInstance;
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  savedGlobal = (globalThis as any)[GLOBAL];
  try { delete (globalThis as any)[GLOBAL]; } catch { /* a slot undici defined cannot be deleted */ } (globalThis as any)[GLOBAL] = undefined; // each test starts with Node's own default: an empty slot
  process.env.FI_API_KEY = "env-key-synthetic-0002"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0002";
  fetchSpy = jest.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 200, json: async () => ({ result: { exists: false } }), text: async () => "" } as Response);
});
afterEach(() => {
  fetchSpy.mockRestore(); for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; }
  if (savedGlobal === undefined) delete (globalThis as any)[GLOBAL]; else (globalThis as any)[GLOBAL] = savedGlobal;
});

const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
// Stand-ins for undici's dispatchers, named as undici names them (the SDK reads the name: undici is not a dependency).
class ProxyAgent {}
class EnvHttpProxyAgent {}
const check = (base: string, dispatcher?: unknown) => {
  process.env.FI_BASE_URL = base;
  return (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { allowInsecureHttp: true, ...(dispatcher === undefined ? {} : { dispatcher }) });
};
const PROXY = "http://proxy.internal.example:3128";

describe("a caller's dispatcher: proxied when the library or Python says so", () => {
  test("HTTP_PROXY with NO_PROXY='.example.com' for http://example.com/ through an environment-proxy dispatcher", async () => {
    process.env.HTTP_PROXY = PROXY; process.env.NO_PROXY = ".example.com";
    await check("http://example.com", new EnvHttpProxyAgent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("ALL_PROXY alone", async () => {
    process.env.ALL_PROXY = PROXY;
    await check("http://collector.example:8000", new EnvHttpProxyAgent());
    expect(fetchSpy).not.toHaveBeenCalled();
    delete process.env.ALL_PROXY; process.env.all_proxy = PROXY;
    await check("http://collector.example:8000", new Agent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("a lower-case and upper-case mix", async () => {
    process.env.HTTP_PROXY = PROXY; process.env.NO_PROXY = "collector.example"; process.env.no_proxy = ""; // Python reads the empty lower-case value as "no exemptions"
    await check("http://collector.example:8000", new EnvHttpProxyAgent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("NO_PROXY with a port that is not the request's", async () => {
    process.env.HTTP_PROXY = PROXY; process.env.NO_PROXY = "collector.example:80";
    await check("http://collector.example:8000", new Agent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("a CIDR NO_PROXY", async () => {
    process.env.HTTP_PROXY = PROXY; process.env.NO_PROXY = "10.0.0.0/8";
    await check("http://10.1.2.3:8000", new Agent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("a proxy agent", async () => {
    await check("http://collector.example:8000", new ProxyAgent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("a dispatcher that cannot be inspected", async () => {
    for (const dispatcher of [new Proxy({}, {}), () => undefined, Object.create(null), "dispatcher", 42, Object.defineProperty({}, "constructor", { get(): never { throw new Error("no"); } })]) {
      await check("http://collector.example:8000", dispatcher);
    }
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("a plain dispatcher with no proxy in the environment is refused: a caller's dispatcher is never accepted", async () => {
    await check("http://collector.example:8000", new Agent());
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("the global dispatcher is read for each request", () => {
  test("a proxy agent set after the check module was loaded is refused", async () => {
    await check("http://collector.example:8000");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    (globalThis as any)[GLOBAL] = new ProxyAgent();
    await check("http://collector.example:8000");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    (globalThis as any)[GLOBAL] = new Agent();
    await check("http://collector.example:8000");
    expect(fetchSpy).toHaveBeenCalledTimes(1); // an Agent the application installed is not Node's own either
  });
  test("the deprecated exporter reads it when it sends, not when it is built", async () => {
    const exporter = new HTTPSpanExporter({ endpoint: "http://collector.example:8000/tracer/v1/traces", headers: { ...KEYS }, allowInsecureHttp: true } as any);
    (globalThis as any)[GLOBAL] = new ProxyAgent();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    provider.getTracer("t").startSpan("one").end();
    await provider.forceFlush(); await provider.shutdown();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("an environment-proxy global dispatcher follows its own rule", async () => {
    (globalThis as any)[GLOBAL] = new EnvHttpProxyAgent();
    process.env.HTTP_PROXY = PROXY;
    await check("http://collector.example:8000");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("Node's own environment-proxy switch counts as a proxy for fetch", async () => {
    process.env.NODE_USE_ENV_PROXY = "1"; process.env.HTTP_PROXY = PROXY;
    await check("http://collector.example:8000");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

async function listener(): Promise<{ port: number; connections: number; close(): Promise<void> }> {
  const state = { port: 0, connections: 0, close: async () => {} };
  const server = net.createServer(socket => { state.connections++; socket.destroy(); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.port = (server.address() as AddressInfo).port;
  state.close = () => new Promise<void>(done => server.close(() => done()));
  return state;
}

describe("a request the SDK builds itself goes direct, with a proxy in the environment", () => {
  // 127.1 is not the literal loopback: a self-hosted host that needs the opt-in, and the connection still reaches this machine.
  test("HTTP exporter", async () => {
    const proxy = await listener();
    const received: http.IncomingHttpHeaders[] = [];
    const server = http.createServer((req, res) => { req.resume(); req.on("end", () => { received.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); }); });
    await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
    try {
      process.env.HTTP_PROXY = `http://127.0.0.1:${proxy.port}`; process.env.http_proxy = process.env.HTTP_PROXY; process.env.ALL_PROXY = process.env.HTTP_PROXY;
      const exporter = createSpanExporter({ endpoint: `http://127.1:${(server.address() as AddressInfo).port}`, headers: { ...KEYS }, allowInsecureHttp: true });
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      provider.getTracer("t").startSpan("one").end();
      await provider.forceFlush(); await provider.shutdown();
      expect(received).toHaveLength(1);
      expect(received[0]["x-api-key"]).toBe(KEYS["x-api-key"]);
      expect(proxy.connections).toBe(0);
    } finally { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); await proxy.close(); }
  });
  test("gRPC exporter", async () => {
    const proxy = await listener();
    const received: grpc.Metadata[] = [];
    const server = new grpc.Server();
    const id = (b: Buffer) => b;
    server.addService({ Export: { path: "/opentelemetry.proto.collector.trace.v1.TraceService/Export", requestStream: false, responseStream: false,
      requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } },
    { Export: (call: any, callback: any) => { received.push(call.metadata); callback(null, Buffer.alloc(0)); } });
    const port = await new Promise<number>((ok, bad) => server.bindAsync("127.0.0.1:0", grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
    try {
      process.env.HTTP_PROXY = `http://127.0.0.1:${proxy.port}`; process.env.http_proxy = process.env.HTTP_PROXY; process.env.https_proxy = process.env.HTTP_PROXY; process.env.grpc_proxy = process.env.HTTP_PROXY;
      const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: `http://127.1:${port}`, headers: { ...KEYS }, allowInsecureHttp: true });
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      provider.getTracer("t").startSpan("one").end();
      await provider.forceFlush(); await provider.shutdown();
      expect(received).toHaveLength(1);
      expect(received[0].get("x-api-key")).toEqual([KEYS["x-api-key"]]);
      expect(proxy.connections).toBe(0);
    } finally { server.forceShutdown(); await proxy.close(); }
  });
});
