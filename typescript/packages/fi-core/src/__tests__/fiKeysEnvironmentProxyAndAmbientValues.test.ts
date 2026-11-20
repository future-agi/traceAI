import * as grpc from "@grpc/grpc-js";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter, Transport } from "../index";
import { isRedirect } from "../fi-request-policy";
import * as secrets from "../secrets";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

/**
 * Environment-reading dispatchers always count as proxied, an SDK-built fetch gets one outcome whether or not the global
 * dispatcher slot is filled, and the ambient-header value check compares only against the Future AGI key values.
 */
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
  "NO_PROXY", "no_proxy", "NODE_USE_ENV_PROXY", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"];
const GLOBAL = Symbol.for("undici.globalDispatcher.1");
let saved: Record<string, string | undefined> = {};
let savedGlobal: unknown;
let fetchSpy: jest.SpyInstance;
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  savedGlobal = (globalThis as any)[GLOBAL]; delete (globalThis as any)[GLOBAL];
  process.env.FI_API_KEY = "env-key-synthetic-0002"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0002";
  fetchSpy = jest.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 200, json: async () => ({ result: { exists: false } }), text: async () => "" } as Response);
});
afterEach(() => {
  fetchSpy.mockRestore(); for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; }
  if (savedGlobal === undefined) delete (globalThis as any)[GLOBAL]; else (globalThis as any)[GLOBAL] = savedGlobal;
});
const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
class Agent {}
class EnvHttpProxyAgent { constructor(public options: Record<string, unknown> = {}) {} }
const send = (dispatcher?: unknown) => {
  process.env.FI_BASE_URL = "http://collector.example:8000";
  return (checkCustomEvalConfigExists as any)("p", [tag()], undefined, false, { allowInsecureHttp: true, ...(dispatcher === undefined ? {} : { dispatcher }) });
};
const PROXY = "http://proxy.internal.example:3128";

describe("an environment-reading dispatcher is always proxied", () => {
  test("an explicit httpProxy option with no proxy in the environment is refused", async () => {
    await send(new EnvHttpProxyAgent({ httpProxy: PROXY }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("an agent built while HTTP_PROXY was set and used after it was unset is refused, as the global dispatcher too", async () => {
    process.env.HTTP_PROXY = PROXY;
    const agent = new EnvHttpProxyAgent();
    delete process.env.HTTP_PROXY;
    await send(agent);
    expect(fetchSpy).not.toHaveBeenCalled();
    (globalThis as any)[GLOBAL] = agent;
    await send();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("Node's environment-proxy switch is refused whatever the proxy variables say", async () => {
    process.env.NODE_USE_ENV_PROXY = "1";
    await send();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("one outcome for an SDK-built fetch with a proxy in the environment", () => {
  test("an empty global slot and a plain Agent installed give the same result", async () => {
    process.env.HTTP_PROXY = PROXY;
    await send();
    const empty = fetchSpy.mock.calls.length;
    (globalThis as any)[GLOBAL] = new Agent();
    await send();
    const plain = fetchSpy.mock.calls.length - empty;
    expect(plain).toBe(empty);
  });
});

describe("the ambient-header value check judges only the Future AGI key values", () => {
  const http = (extra: Record<string, unknown> = {}) => createSpanExporter({ endpoint: "http://collector.example:4318", allowInsecureHttp: true, ...extra } as any);
  const rpc = (extra: Record<string, unknown> = {}) => createSpanExporter({ transport: Transport.GRPC, endpoint: "collector.example:4317", credentials: grpc.credentials.createInsecure(), ...extra } as any);
  test("another vendor's key in the ambient headers for a non-FI collector is allowed", async () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-vendor-token=vendor-secret-value-0099";
    process.env.VENDOR_API_TOKEN = "vendor-secret-value-0099";
    try {
      await http({ headers: { "x-vendor-header": "vendor-secret-value-0099" } }).shutdown();
      await rpc().shutdown();
    } finally { delete process.env.VENDOR_API_TOKEN; }
  });
  test("a registered secret that is not a Future AGI key is allowed", async () => {
    secrets.registerSecret("registered-other-0098");
    process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS = "x-other=registered-other-0098";
    await http().shutdown();
  });
  test("the FI_SECRET_KEY value under any header name is refused, plain or percent-encoded", () => {
    process.env.FI_SECRET_KEY = "env secret/synthetic+0002";
    for (const value of ["env secret/synthetic+0002", encodeURIComponent("env secret/synthetic+0002"), "prefix-" + encodeURIComponent("env secret/synthetic+0002")]) {
      process.env.OTEL_EXPORTER_OTLP_HEADERS = `x-anything=${value}`;
      expect(() => http()).toThrow();
      expect(() => rpc()).toThrow();
    }
  });
  test("a key value from the headers option under another name in the ambient headers is refused", () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-anything=option-key-synthetic-0007";
    expect(() => http({ headers: { "x-api-key": "option-key-synthetic-0007" } })).toThrow();
  });
  test("the check runs only when an OTEL header variable is set, and never throws a TypeError or reads a failed registry", async () => {
    secrets.markSecretDiscoveryFailed();
    try {
      await http({ headers: { "x-undefined": undefined } }).shutdown();
      process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-benign=value-synthetic-0001";
      let error: unknown;
      try { http({ headers: { "x-undefined": undefined } }); } catch (e) { error = e; }
      expect(error instanceof TypeError).toBe(false);
    } finally { delete (globalThis as any)[Symbol.for("traceai.secrets")]; }
  });
});

describe("redirects", () => {
  test("an opaqueredirect response counts as a redirect", () => {
    expect(isRedirect({ type: "opaqueredirect", status: 0 })).toBe(true);
    expect(isRedirect({ status: 200 })).toBe(false);
  });
});
