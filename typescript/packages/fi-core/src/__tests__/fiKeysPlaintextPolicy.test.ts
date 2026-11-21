import * as grpc from "@grpc/grpc-js";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter, HTTPSpanExporter, register, Transport } from "../otel";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

/**
 * The plaintext rule for every request that carries FI keys:
 *   loopback                      plaintext allowed, no opt-in
 *   a Future AGI host             always TLS (gRPC) or https (HTTP); the opt-in never applies
 *   any other host, plaintext     refused unless FI_ALLOW_INSECURE_HTTP=1 or allowInsecureHttp: true,
 *                                 and then never through an HTTP(S) proxy; one warning per process
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE", "FI_API_KEY", "FI_SECRET_KEY", "FI_GRPC_COLLECTOR_ENDPOINT", "FI_GRPC_URL", "FI_BASE_URL", "FI_TRACES_URL", "FI_COLLECTOR_ENDPOINT",
  "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "GRPC_PROXY", "grpc_proxy", "no_grpc_proxy"];
let saved: Record<string, string | undefined> = {};
let fetchSpy: jest.SpyInstance;
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  process.env.FI_API_KEY = "env-key-synthetic-0002"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0002";
  // A slot that undici defined cannot be deleted; an empty value is Node's own default.
  savedSlot = (globalThis as any)[SLOT]; (globalThis as any)[SLOT] = undefined;
  fetchSpy = jest.spyOn(require("../fi-transport"), "ownFetch").mockResolvedValue({ ok: true, json: async () => ({ result: { exists: false } }), text: async () => "" } as Response);
});
const SLOT = Symbol.for("undici.globalDispatcher.1");
let savedSlot: unknown;
afterEach(() => { (globalThis as any)[SLOT] = savedSlot; fetchSpy.mockRestore(); for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
const http = (endpoint: string, extra: Record<string, unknown> = {}) => createSpanExporter({ endpoint, headers: { ...KEYS }, ...extra } as any);
const rpc = (endpoint: string, extra: Record<string, unknown> = {}) => createSpanExporter({ transport: Transport.GRPC, endpoint, headers: { ...KEYS }, ...extra } as any);
function channel(exporter: unknown): { address: string; secure: boolean } {
  const parameters = (exporter as any)._delegate._transport._parameters;
  return { address: parameters.address, secure: parameters.credentials()._isSecure() };
}
function refused(build: () => unknown): string {
  let message = "";
  try { build(); } catch (error) { message = String((error as Error).message); }
  return message;
}
/** Warnings the SDK logs while `run` executes, in a fresh copy of the module (the warning is once per process). */
function withFreshModule<T>(run: (m: typeof import("../otel"), warnings: string[]) => T): { result: T; warnings: string[] } {
  const warnings: string[] = [];
  let result!: T;
  jest.isolateModules(() => {
    const api = require("@opentelemetry/api");
    api.diag.setLogger({ error: () => {}, warn: (m: string) => warnings.push(m), info: () => {}, debug: () => {}, verbose: () => {} }, { logLevel: DiagLogLevel.WARN, suppressOverrideMessage: true });
    // A fresh copy of the package loads its own copy of undici: its fetch is routed to the test's spy so sends are counted.
    jest.spyOn(require("../fi-transport"), "ownFetch").mockImplementation(((...args: unknown[]) => (fetchSpy as any)(...args)) as any);
    result = run(require("../otel"), warnings);
  });
  diag.disable();
  return { result, warnings };
}

describe("loopback keeps the plaintext channel without an opt-in", () => {
  test.each(["http://127.0.0.1:1", "http://localhost:1", "http://[::1]:1"])("HTTP %s with keys", async endpoint => {
    await http(endpoint).shutdown();
    await new HTTPSpanExporter({ endpoint: `${endpoint}/tracer/v1/traces`, headers: { ...KEYS } } as any).shutdown();
  });
  test.each(["localhost:4317", "127.0.0.1:4317", "[::1]:4317"])("gRPC %s with keys", endpoint => {
    expect(channel(rpc(endpoint)).secure).toBe(false);
  });
});

describe("a Future AGI host is never used in plaintext, and the opt-in does not change that", () => {
  test.each([
    ["http://api.futureagi.com"], ["http://api.futureagi.com./"], ["http://eu.futureagi.com:8080"], ["http://API.FUTUREAGI.COM"],
  ])("HTTP %s is refused by default, with FI_ALLOW_INSECURE_HTTP=1 and with allowInsecureHttp", endpoint => {
    expect(refused(() => http(endpoint))).not.toBe("");
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(refused(() => http(endpoint))).not.toBe("");
    expect(refused(() => http(endpoint, { allowInsecureHttp: true }))).not.toBe("");
  });
  test.each([["http://grpc.futureagi.com:4317"], ["grpc.futureagi.com.:4317"], ["http://grpc.futureagi.com.:4317"]])("gRPC %s is refused when a plaintext channel is asked for, whatever else says", endpoint => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env.OTEL_EXPORTER_OTLP_INSECURE = "true";
    expect(() => rpc(endpoint, { credentials: grpc.credentials.createInsecure(), allowInsecureHttp: true })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
  });
  test("the refusals name no value", () => {
    const message = refused(() => http("http://api.futureagi.com"));
    expect(message).not.toMatch(/synthetic|futureagi/);
  });
});

describe("another host in plaintext with FI keys is refused by default", () => {
  test.each(["http://collector.example:4318", "http://203.0.113.7:4318/v1/traces", "http://host.docker.internal:4318", "http://0.0.0.0:4318"])("HTTP %s", endpoint => {
    const message = refused(() => http(endpoint));
    expect(message).toMatch(/FI_ALLOW_INSECURE_HTTP/);
    expect(message).not.toMatch(/synthetic|collector\.example|203\.0|docker/);
  });
  test("HTTP: a base URL named by FI_BASE_URL with the environment keys", () => {
    process.env.FI_BASE_URL = "http://selfhosted.example:8000";
    expect(refused(() => createSpanExporter({}))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
  });
  test("HTTP: the deprecated exporter", () => {
    expect(refused(() => new HTTPSpanExporter({ endpoint: "http://collector.example:4318/tracer/v1/traces", headers: { ...KEYS } } as any))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
  });
  test("HTTP without FI keys is a plain collector", async () => {
    await createSpanExporter({ endpoint: "http://collector.example:4318" }).shutdown();
  });
  test.each([
    ["http:// scheme", { endpoint: "http://collector.example:4317" }, {}],
    ["OTEL_EXPORTER_OTLP_INSECURE=true", { endpoint: "collector.example:4317" }, { OTEL_EXPORTER_OTLP_INSECURE: "true" }],
    ["OTEL_EXPORTER_OTLP_TRACES_INSECURE=true", { endpoint: "collector.example:4317" }, { OTEL_EXPORTER_OTLP_TRACES_INSECURE: "true" }],
    ["insecure credentials", { endpoint: "collector.example:4317", credentials: grpc.credentials.createInsecure() }, {}],
    ["a self-hosted target named by the environment", {}, { FI_GRPC_COLLECTOR_ENDPOINT: "collector.example:4317", OTEL_EXPORTER_OTLP_INSECURE: "true" }],
  ])("gRPC with %s", (_name, options, env) => {
    Object.assign(process.env, env);
    const endpoint = (options as any).endpoint as string | undefined;
    expect(refused(() => endpoint === undefined ? createSpanExporter({ transport: Transport.GRPC, headers: { ...KEYS } } as any) : rpc(endpoint, options))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
  });
  test("gRPC over TLS to a self-hosted collector is unaffected", () => {
    expect(channel(rpc("collector.example:4317")).secure).toBe(true);
    expect(channel(rpc("https://collector.example:4317")).secure).toBe(true);
  });
});

describe("the opt-in allows plaintext to a host that is not Future AGI, with one warning", () => {
  test("the exact value 1, or the option, allows HTTP; any other value does not", () => {
    process.env.FI_ALLOW_INSECURE_HTTP = "true";
    expect(refused(() => http("http://collector.example:4318"))).not.toBe("");
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(refused(() => http("http://collector.example:4318"))).toBe("");
    delete process.env.FI_ALLOW_INSECURE_HTTP;
    expect(refused(() => http("http://collector.example:4318", { allowInsecureHttp: true }))).toBe("");
    expect(refused(() => http("http://collector.example:4318", { allowInsecureHttp: "yes" }))).not.toBe("");
  });
  test("gRPC plaintext is allowed by the opt-in", () => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(channel(rpc("http://collector.example:4317"))).toEqual({ address: "collector.example:4317", secure: false });
    expect(channel(rpc("collector.example:4317", { credentials: grpc.credentials.createInsecure() })).secure).toBe(false);
  });
  test("one warning per process, naming the setting and the origin only", () => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    const { warnings } = withFreshModule(m => {
      m.createSpanExporter({ endpoint: "http://collector.example:4318/tracer/v1/traces?token=query-synthetic", headers: { ...KEYS } } as any);
      m.createSpanExporter({ endpoint: "http://other.example:4318", headers: { ...KEYS } } as any);
      m.createSpanExporter({ transport: m.Transport.GRPC, endpoint: "http://third.example:4317", headers: { ...KEYS } } as any);
    });
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toMatch(/FI_ALLOW_INSECURE_HTTP/);
    expect(warnings[0]).toContain("http://collector.example:4318");
    expect(warnings[0]).not.toMatch(/user|pw-synthetic|query-synthetic|token|tracer\/v1|synthetic/);
  });
  test("loopback needs no opt-in and logs no warning", () => {
    const { warnings } = withFreshModule(m => { m.createSpanExporter({ endpoint: "http://127.0.0.1:4318", headers: { ...KEYS } } as any); });
    expect(warnings).toHaveLength(0);
  });
});

describe("a request the SDK builds itself goes direct, whatever the proxy environment says", () => {
  test.each(["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"])("%s does not refuse or route HTTP", name => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env[name] = "http://proxy.internal.example:3128";
    expect(refused(() => http("http://collector.example:4318"))).toBe("");
    expect(refused(() => http("http://collector.example:4318", { allowInsecureHttp: true }))).toBe("");
  });
  test.each(["http_proxy", "https_proxy", "grpc_proxy"])("%s does not refuse gRPC, whose channel is built with proxies off", name => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env[name] = "http://proxy.internal.example:3128";
    expect(refused(() => rpc("http://collector.example:4317"))).toBe("");
  });
  test("a caller's dispatcher is where the proxy rules apply (see the decision table)", async () => {
    class ProxyAgent {}
    process.env.FI_BASE_URL = "http://selfhosted.example:8000"; process.env.FI_ALLOW_INSECURE_HTTP = "1";
    await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { dispatcher: new ProxyAgent() });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("the route factory used by @traceai/auto follows the same rules", () => {
  const factory = () => (globalThis as any)[Symbol.for("traceai.fi-core.fi-route-factory.v1")];
  test("a self-hosted http:// base is refused unless the option allows it", () => {
    expect(refused(() => factory()({ endpoint: "http://collector.example:4318", headers: { ...KEYS } }))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
    expect(refused(() => factory()({ endpoint: "http://collector.example:4318", headers: { ...KEYS }, allowInsecureHttp: true }))).toBe("");
    expect(refused(() => factory()({ endpoint: "http://api.futureagi.com", headers: { ...KEYS }, allowInsecureHttp: true }))).not.toBe("");
  });
});

describe("the custom eval checks follow the same rules", () => {
  test("self-hosted http base: no request by default", async () => {
    process.env.FI_BASE_URL = "http://selfhosted.example:8000";
    expect(await checkCustomEvalConfigExists("p", [tag()])).toBe(false);
    expect(refused(() => register({ projectName: "p", evalTags: [tag() as any], setGlobalTracerProvider: false }))).not.toBe("");
    await new Promise(done => setTimeout(done, 100));
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("with the opt-in the check is made, with one warning", async () => {
    process.env.FI_BASE_URL = "http://selfhosted.example:8000"; process.env.FI_ALLOW_INSECURE_HTTP = "1";
    const { result, warnings } = withFreshModule(m => m.checkCustomEvalConfigExists("p", [tag()]));
    await result;
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(warnings.filter(w => /FI_ALLOW_INSECURE_HTTP/.test(w))).toHaveLength(1);
    expect(await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { allowInsecureHttp: true })).toBe(false);
  });
  test("an http:// Future AGI base is never checked, with the opt-in either", async () => {
    process.env.FI_BASE_URL = "http://api.futureagi.com"; process.env.FI_ALLOW_INSECURE_HTTP = "1";
    await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { allowInsecureHttp: true });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("a request the SDK builds itself goes direct: with a proxy variable set and an empty global dispatcher slot (the default Agent) the check is sent", async () => {
    process.env.FI_BASE_URL = "http://selfhosted.example:8000"; process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env.HTTP_PROXY = "http://proxy.internal.example:3128";
    delete (globalThis as any)[Symbol.for("undici.globalDispatcher.1")];
    await checkCustomEvalConfigExists("p", [tag()]);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
  test("with a proxy variable set and a global dispatcher that can read a proxy, the check is sent through fi-core's own dispatcher, never the global one", async () => {
    process.env.FI_BASE_URL = "http://selfhosted.example:8000"; process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env.HTTP_PROXY = "http://proxy.internal.example:3128";
    class EnvHttpProxyAgent {}
    const slot = Symbol.for("undici.globalDispatcher.1"), saved = (globalThis as any)[slot], global = new EnvHttpProxyAgent();
    (globalThis as any)[slot] = global;
    try { await checkCustomEvalConfigExists("p", [tag()]); } finally { if (saved === undefined) delete (globalThis as any)[slot]; else (globalThis as any)[slot] = saved; }
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const init = fetchSpy.mock.calls[0][1] as { dispatcher?: unknown };
    expect(init.dispatcher).toBeDefined(); expect(init.dispatcher).not.toBe(global);
  });
  test("loopback and https bases are checked", async () => {
    process.env.FI_BASE_URL = "http://127.0.0.1:9"; await checkCustomEvalConfigExists("p", [tag()]);
    process.env.FI_BASE_URL = "https://selfhosted.example"; await checkCustomEvalConfigExists("p", [tag()]);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});

/**
 * Loopback is decided on the host as written in the URL or target, before any parser rewrites it: only the literal
 * localhost, 127.0.0.1 and ::1 / [::1] count. A spelling a parser turns into loopback is another host: it is refused with
 * FI keys over plaintext, allowed (with the one warning) under the opt-in, and still refused through a proxy.
 */
describe("only a literal loopback host is loopback", () => {
  const WRITTEN = ["127.0.0.1.", "127.1", "0x7f.0.0.1", "2130706433", "127.0.0.01", "0177.0.0.1", "LOCALHOST.", "localhost."];
  test.each(["localhost", "LOCALHOST", "127.0.0.1", "[::1]"])("%s stays loopback on HTTP and gRPC", async host => {
    await http(`http://${host}:4318`).shutdown();
    expect(channel(rpc(`${host}:4317`)).secure).toBe(false);
    expect(channel(rpc(`http://${host}:4317`)).secure).toBe(false);
  });
  test.each(WRITTEN)("(a) %s: HTTP and gRPC plaintext with FI keys are refused by default", host => {
    expect(refused(() => http(`http://${host}:4318`))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
    expect(refused(() => rpc(`http://${host}:4317`))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
    expect(refused(() => rpc(`${host}:4317`, { credentials: grpc.credentials.createInsecure() }))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
  });
  test.each(WRITTEN)("(b) %s: allowed by the opt-in as a self-hosted host, with one warning", host => {
    const { warnings } = withFreshModule(m => {
      process.env.FI_ALLOW_INSECURE_HTTP = "1";
      m.createSpanExporter({ endpoint: `http://${host}:4318`, headers: { ...KEYS } } as any);
      delete process.env.FI_ALLOW_INSECURE_HTTP;
      m.createSpanExporter({ endpoint: `http://${host}:4318`, headers: { ...KEYS }, allowInsecureHttp: true } as any);
      const exporter = m.createSpanExporter({ transport: m.Transport.GRPC, endpoint: `http://${host}:4317`, headers: { ...KEYS }, allowInsecureHttp: true } as any);
      expect(channel(exporter).secure).toBe(false);
    });
    expect(warnings.filter(w => /FI_ALLOW_INSECURE_HTTP/.test(w))).toHaveLength(1);
  });
  test.each(WRITTEN)("(b) %s: still refused through a caller's proxy dispatcher", async host => {
    class ProxyAgent {}
    process.env.FI_BASE_URL = `http://${host}:8000`;
    await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { allowInsecureHttp: true, dispatcher: new ProxyAgent() });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test.each(WRITTEN)("%s as an eval-check base: no request by default, a request with the opt-in", async host => {
    process.env.FI_BASE_URL = `http://${host}:8000`;
    await checkCustomEvalConfigExists("p", [tag()]);
    expect(fetchSpy).not.toHaveBeenCalled();
    await (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { allowInsecureHttp: true });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
  test("a Future AGI host is decided the same way: never loopback, never plaintext", () => {
    for (const host of ["api.futureagi.com.", "API.FUTUREAGI.COM"]) {
      expect(refused(() => http(`http://${host}`, { allowInsecureHttp: true }))).not.toBe("");
    }
  });
});
