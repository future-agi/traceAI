import * as grpc from "@grpc/grpc-js";
import net from "node:net";
import { AddressInfo } from "node:net";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter, FITracerProvider, Transport } from "../otel";
import { decideFiRequest, FiKeyRefusal } from "../fi-request-policy";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";
import { Agent } from "undici";

/**
 * Loopback keeps only the plaintext opt-in exemption: a plaintext request that carries Future AGI keys is never proxied, loopback
 * included. A caller's proxy dispatcher, a global environment-reading dispatcher and NODE_USE_ENV_PROXY are refused for loopback
 * as for any other host, and a plaintext gRPC channel is always built with proxies off. Caller TLS credentials are never replaced
 * with insecure ones. Every cell with a proxy asserts that the proxy listener saw no bytes.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
  "NO_PROXY", "no_proxy", "GRPC_PROXY", "grpc_proxy", "no_grpc_proxy", "NODE_USE_ENV_PROXY", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"];
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

class ProxyAgent {}
class EnvHttpProxyAgent {}
const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
const check = (base: string, dispatcher?: unknown, options: Record<string, unknown> = {}) => {
  process.env.FI_BASE_URL = base;
  return (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { ...(dispatcher === undefined ? {} : { dispatcher }), ...options });
};

/** A TCP listener that stands in for a proxy and counts what reaches it. */
async function proxyListener(): Promise<{ url: string; bytes: number; connections: number; close(): Promise<void> }> {
  const state = { url: "", bytes: 0, connections: 0, close: async () => {} };
  const sockets = new Set<net.Socket>();
  const server = net.createServer(socket => { state.connections++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.on("data", chunk => { state.bytes += chunk.length; }); socket.on("error", () => {}); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  state.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  state.close = () => new Promise<void>(done => { for (const socket of sockets) socket.destroy(); server.close(() => done()); });
  return state;
}
const LOOPBACKS = ["http://127.0.0.1:4318", "http://localhost:4318", "http://[::1]:4318"];

describe("a plaintext loopback send never goes through a proxy", () => {
  test("a caller's ProxyAgent with http://127.0.0.1 is refused FI_PLAINTEXT_PROXIED, and the proxy sees nothing", async () => {
    const proxy = await proxyListener();
    try {
      process.env.HTTP_PROXY = proxy.url;
      expect(() => decideFiRequest({ target: "http://127.0.0.1:4318", transport: "http", headers: { ...KEYS }, dispatcher: new ProxyAgent() })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_PROXIED" }));
      await check("http://127.0.0.1:4318", new ProxyAgent());
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(proxy.bytes).toBe(0);
    } finally { await proxy.close(); }
  });
  test("a global EnvHttpProxyAgent with http://localhost is refused or sent direct, and the proxy sees nothing", async () => {
    const proxy = await proxyListener();
    try {
      process.env.HTTP_PROXY = proxy.url;
      (globalThis as any)[GLOBAL] = new EnvHttpProxyAgent();
      await check("http://localhost:4318");
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(proxy.bytes).toBe(0);
    } finally { await proxy.close(); }
  });
  test("NODE_USE_ENV_PROXY=1 with http://[::1] is refused or sent direct, and the proxy sees nothing", async () => {
    const proxy = await proxyListener();
    try {
      process.env.NODE_USE_ENV_PROXY = "1"; process.env.HTTP_PROXY = proxy.url;
      await check("http://[::1]:4318");
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(proxy.bytes).toBe(0);
    } finally { await proxy.close(); }
  });
  test.each(LOOPBACKS)("a send with no caller dispatcher and no environment proxy to %s is not refused", async base => {
    await check(base);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
  test.each(LOOPBACKS)("HTTP_PROXY set does not refuse a send to %s that Node sends direct", async base => {
    const proxy = await proxyListener();
    try {
      process.env.HTTP_PROXY = proxy.url; process.env.http_proxy = proxy.url; process.env.ALL_PROXY = proxy.url;
      await check(base);
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      await check(base, new Agent()); // a caller's dispatcher is refused
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(proxy.bytes).toBe(0);
    } finally { await proxy.close(); }
  });
  test("the same rule decides an exporter's request built through a caller dispatcher", () => {
    for (const target of LOOPBACKS) {
      expect(() => decideFiRequest({ target, transport: "http", headers: { ...KEYS }, dispatcher: new EnvHttpProxyAgent() })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_PROXIED" }));
      expect(decideFiRequest({ target, transport: "http", headers: { ...KEYS }, dispatcher: new Agent() }).keys).toBe(true);
    }
  });
});

describe("a plaintext gRPC channel that carries keys is built with proxies off, loopback included", () => {
  test("http://127.0.0.1 with grpc_proxy and http_proxy set goes direct and the proxy sees nothing", async () => {
    const proxy = await proxyListener();
    const received: grpc.Metadata[] = [];
    const server = new grpc.Server();
    const id = (b: Buffer) => b;
    server.addService({ Export: { path: "/opentelemetry.proto.collector.trace.v1.TraceService/Export", requestStream: false, responseStream: false,
      requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } },
    { Export: (call: any, callback: any) => { received.push(call.metadata); callback(null, Buffer.alloc(0)); } });
    const port = await new Promise<number>((ok, bad) => server.bindAsync("127.0.0.1:0", grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
    try {
      process.env.HTTP_PROXY = proxy.url; process.env.http_proxy = proxy.url; process.env.https_proxy = proxy.url; process.env.grpc_proxy = proxy.url;
      const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: `http://127.0.0.1:${port}`, headers: { ...KEYS } });
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      provider.getTracer("t").startSpan("one").end();
      await provider.forceFlush(); await provider.shutdown();
      expect(received).toHaveLength(1);
      expect(received[0].get("x-api-key")).toEqual([KEYS["x-api-key"]]);
      expect(proxy.connections).toBe(0);
      expect(proxy.bytes).toBe(0);
    } finally { server.forceShutdown(); await proxy.close(); }
  });
  test("the decision reports a direct build for every plaintext key-carrying gRPC target, and not for TLS or no keys", () => {
    for (const target of ["127.0.0.1:4317", "http://localhost:4317", "http://[::1]:4317"]) {
      const decision = decideFiRequest({ target, transport: "grpc", headers: { ...KEYS } }) as any;
      expect(decision.tls).toBe(false);
      expect(decision.plaintextDirect).toBe(true);
    }
    expect((decideFiRequest({ target: "collector.example:4317", transport: "grpc", headers: { ...KEYS } }) as any).plaintextDirect).toBe(false);
    expect((decideFiRequest({ target: "127.0.0.1:4317", transport: "grpc" }) as any).plaintextDirect).toBe(false);
  });
});

describe("a caller's TLS credentials are never replaced with insecure ones", () => {
  test.each(["localhost:4317", "127.0.0.1:4317", "https://localhost:4317"])("caller TLS credentials with an FI-bound loopback target %s", target => {
    const credentials = grpc.credentials.createSsl();
    let decision: any, error: any;
    try { decision = decideFiRequest({ target, transport: "grpc", headers: { ...KEYS }, credentials, declaredFi: true }); } catch (e) { error = e; }
    if (error) { expect(error).toBeInstanceOf(FiKeyRefusal); return; }
    expect(decision.tls).toBe(true);
    expect(decision.credentials._isSecure()).toBe(true);
  });
  test("through the exporter: the channel stays TLS, or the exporter is refused", () => {
    const credentials = grpc.credentials.createSsl();
    let exporter: any;
    try { exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: "localhost:4317", headers: { ...KEYS }, credentials } as any); } catch (e) { expect(e).toBeInstanceOf(FiKeyRefusal); return; }
    expect(exporter._delegate._transport._parameters.credentials()._isSecure()).toBe(true);
  });
  test("insecure credentials only when the caller supplied none and the target is literal plaintext loopback", () => {
    expect(decideFiRequest({ target: "localhost:4317", transport: "grpc", headers: { ...KEYS }, declaredFi: true }).credentialsKind).toBe("plaintext");
    expect((decideFiRequest({ target: "localhost:4317", transport: "grpc", headers: { ...KEYS }, credentials: grpc.credentials.createInsecure() }) as any).credentials._isSecure()).toBe(false);
  });
});

describe("the ambient-header check reads every value of a repeated key-named gRPC metadata entry", () => {
  test("the second value under x-api-key in an ambient header refuses the exporter", () => {
    const metadata = new grpc.Metadata();
    metadata.add("x-api-key", "first-key-value-synthetic-0001"); metadata.add("x-api-key", "second-key-value-synthetic-0002");
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-trace-token=second-key-value-synthetic-0002";
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: "https://collector.example:4317", metadata, headers: {} } as any)).toThrow(expect.objectContaining({ code: "FI_AMBIENT_KEY_HEADERS" }));
  });
});

describe("FITracerProvider runs the key decision only when an SDK exporter that carries keys is built", () => {
  test("explicit span processors and a plaintext target build, with no exporter of its own", () => {
    expect(() => new FITracerProvider({ endpoint: "http://collector.example:4318", headers: { ...KEYS }, spanProcessors: [] })).not.toThrow();
    expect(() => new FITracerProvider({ transport: Transport.GRPC, endpoint: "http://collector.example:4317", headers: { ...KEYS }, spanProcessors: [] })).not.toThrow();
  });
  test("the default exporter still goes through the decision", () => {
    expect(() => new FITracerProvider({ endpoint: "http://collector.example:4318", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_NOT_ALLOWED" }));
  });
});

describe("refusal messages say Future AGI key values", () => {
  test("the ambient-header refusal does not call anything a known secret or credential", () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-api-key=anything";
    let error: any;
    try { createSpanExporter({ endpoint: "https://collector.example:4318", headers: { authorization: "Bearer x" } } as any); } catch (e) { error = e; }
    expect(error.code).toBe("FI_AMBIENT_KEY_HEADERS");
    expect(error.message).toMatch(/Future AGI key value/);
    expect(error.message).not.toMatch(/known (secret|credential)/i);
  });
});

describe("verbose eval-check logs name the endpoint's host class only", () => {
  const messages: string[] = [];
  beforeEach(() => { messages.length = 0; const sink = (...args: unknown[]) => { messages.push(args.map(String).join(" ")); };
    diag.setLogger({ verbose: sink, debug: sink, info: sink, warn: sink, error: sink }, DiagLogLevel.ALL); });
  afterEach(() => diag.disable());
  test("an endpoint with userinfo and a query token", async () => {
    process.env.FI_BASE_URL = "https://api.futureagi.com";
    const endpoint = "https://alice:hunter2-synthetic@collector.example:8443/tracer/custom-eval-config/check_exists/?token=query-token-synthetic-0003";
    await (checkCustomEvalConfigExists as any)("p", [tag()], endpoint, true, {});
    const text = messages.join("\n");
    expect(text).not.toMatch(/alice|hunter2|query-token|token=|collector\.example|8443/);
    expect(text).toMatch(/Using custom full endpoint: (Future AGI host|loopback|other host)/);
  });
  test("an endpoint that is not a URL is not echoed", async () => {
    process.env.FI_BASE_URL = "https://api.futureagi.com";
    await (checkCustomEvalConfigExists as any)("p", [tag()], "not a url secret-token-synthetic-0004", true, {});
    const text = messages.join("\n");
    expect(text).not.toMatch(/secret-token-synthetic-0004/);
  });
  test("the three host classes", async () => {
    process.env.FI_BASE_URL = "https://api.futureagi.com";
    for (const [endpoint, label] of [["https://api.futureagi.com/tracer/custom-eval-config/check_exists/", "Future AGI host"], ["http://127.0.0.1:9/tracer/custom-eval-config/check_exists/", "loopback"], ["https://collector.example/tracer/custom-eval-config/check_exists/", "other host"]]) {
      messages.length = 0;
      await (checkCustomEvalConfigExists as any)("p", [tag()], endpoint, true, {});
      expect(messages.join("\n")).toContain(`Using custom full endpoint: ${label}`);
    }
  });
});
