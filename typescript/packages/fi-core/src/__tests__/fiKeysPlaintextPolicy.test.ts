import * as grpc from "@grpc/grpc-js";
import { diag, DiagLogLevel } from "@opentelemetry/api";
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
  "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "GRPC_PROXY", "grpc_proxy"];
let saved: Record<string, string | undefined> = {};
let fetchSpy: jest.SpyInstance;
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  process.env.FI_API_KEY = "env-key-synthetic-0002"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0002";
  fetchSpy = jest.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({ result: { exists: false } }), text: async () => "" } as Response);
});
afterEach(() => { fetchSpy.mockRestore(); for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

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
  test.each([["http://grpc.futureagi.com:4317"], ["grpc.futureagi.com.:4317"], ["http://grpc.futureagi.com.:4317"]])("gRPC %s uses TLS whatever else says", endpoint => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env.OTEL_EXPORTER_OTLP_INSECURE = "true";
    expect(channel(rpc(endpoint, { credentials: grpc.credentials.createInsecure(), allowInsecureHttp: true })).secure).toBe(true);
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
    expect(refused(() => rpc((options as any).endpoint ?? "", options))).toMatch(/FI_ALLOW_INSECURE_HTTP/);
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
      m.createSpanExporter({ endpoint: "http://user:pw-synthetic@collector.example:4318/tracer/v1/traces?token=query-synthetic", headers: { ...KEYS } } as any);
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

describe("the opt-in does not apply when the request would go through a proxy", () => {
  test.each(["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"])("%s refuses HTTP and gRPC", name => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env[name] = "http://proxy.internal.example:3128";
    expect(refused(() => http("http://collector.example:4318"))).not.toBe("");
    expect(refused(() => rpc("http://collector.example:4317"))).not.toBe("");
    expect(refused(() => http("http://collector.example:4318", { allowInsecureHttp: true }))).not.toBe("");
  });
  test("NO_PROXY for the host (or a parent domain, or *) lifts the refusal; loopback never needed it", () => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env.HTTP_PROXY = "http://proxy.internal.example:3128";
    for (const noProxy of ["collector.example", ".example", "other.test,collector.example:4318", "*"]) {
      process.env.NO_PROXY = noProxy;
      expect(refused(() => http("http://collector.example:4318"))).toBe("");
    }
    process.env.NO_PROXY = "other.example";
    expect(refused(() => http("http://collector.example:4318"))).not.toBe("");
    delete process.env.NO_PROXY;
    expect(refused(() => http("http://127.0.0.1:4318"))).toBe("");
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
  test("through a proxy the opt-in does not apply", async () => {
    process.env.FI_BASE_URL = "http://selfhosted.example:8000"; process.env.FI_ALLOW_INSECURE_HTTP = "1"; process.env.HTTPS_PROXY = "http://proxy.internal.example:3128";
    await checkCustomEvalConfigExists("p", [tag()]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("loopback and https bases are checked", async () => {
    process.env.FI_BASE_URL = "http://127.0.0.1:9"; await checkCustomEvalConfigExists("p", [tag()]);
    process.env.FI_BASE_URL = "https://selfhosted.example"; await checkCustomEvalConfigExists("p", [tag()]);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});
