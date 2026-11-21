import * as grpc from "@grpc/grpc-js";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import { checkCustomEvalConfigExists, checkCustomEvalTemplateExists, createSpanExporter, HTTPSpanExporter, register, resolveTraceEndpoint, Transport } from "../otel";
import { isFiEndpoint } from "../fi-endpoint";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE", "FI_API_KEY", "FI_SECRET_KEY", "FI_GRPC_COLLECTOR_ENDPOINT", "FI_GRPC_URL", "FI_BASE_URL", "FI_TRACES_URL", "FI_COLLECTOR_ENDPOINT"];
let saved: Record<string, string | undefined> = {};
let fetchSpy: jest.SpyInstance;
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  process.env.FI_API_KEY = "env-key-synthetic-0002"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0002";
  fetchSpy = jest.spyOn(require("undici"), "fetch").mockResolvedValue({ ok: true, json: async () => ({ result: { exists: false, isUserEvalTemplate: false, evalTemplate: null } }), text: async () => "" } as Response);
});
afterEach(() => { fetchSpy.mockRestore(); for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
const settle = () => new Promise<void>(done => setTimeout(done, 100));
function channel(exporter: unknown): { address: string; secure: boolean } {
  const parameters = (exporter as any)._delegate._transport._parameters;
  return { address: parameters.address, secure: parameters.credentials()._isSecure() };
}

describe("the custom eval checks never put FI keys on plain http", () => {
  test.each(["http://api.futureagi.com", "http://api.futureagi.com./", "http://eu.futureagi.com:8080", "HTTP://API.FUTUREAGI.COM"])("%s selected by FI_BASE_URL: register makes no request", async base => {
    process.env.FI_BASE_URL = base;
    expect(() => register({ projectName: "plain-http", evalTags: [tag() as any], setGlobalTracerProvider: false })).toThrow();
    await settle();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test.each(["http://api.futureagi.com", "http://api.futureagi.com./"])("%s: the checks called directly make no request", async base => {
    process.env.FI_BASE_URL = base;
    expect(await checkCustomEvalConfigExists("plain-http", [tag()])).toBe(false);
    expect(await checkCustomEvalTemplateExists("template")).toEqual({ result: { isUserEvalTemplate: false, evalTemplate: null } });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("an explicit http:// Future AGI endpoint makes no request", async () => {
    await checkCustomEvalConfigExists("plain-http", [tag()], "http://api.futureagi.com");
    await checkCustomEvalTemplateExists("template", false, "http://api.futureagi.com/tracer/custom-eval-config/check_template/");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  test("https Future AGI keeps working, and so do loopback and own bases", async () => {
    await checkCustomEvalConfigExists("plain-http", [tag()], "https://api.futureagi.com");
    process.env.FI_BASE_URL = "http://127.0.0.1:9";
    await checkCustomEvalTemplateExists("template");
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});

describe("a trailing dot is the same Future AGI host for every TLS decision", () => {
  test.each(["http://api.futureagi.com./", "http://api.futureagi.com.:8080"])("%s is refused with FI keys, however it is named", endpoint => {
    expect(() => createSpanExporter({ endpoint, headers: { ...KEYS } })).toThrow();
    process.env.FI_BASE_URL = endpoint;
    expect(() => createSpanExporter({})).toThrow();
  });
  test("an https trailing-dot host is accepted", async () => {
    await createSpanExporter({ endpoint: "https://api.futureagi.com./", headers: { ...KEYS } }).shutdown();
  });
  test("a gRPC target with a trailing dot and explicit keys uses TLS, and a plaintext request for it is refused", () => {
    const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: "grpc.futureagi.com.:4317", headers: { ...KEYS } } as any);
    expect(channel(exporter)).toEqual({ address: "grpc.futureagi.com.:4317", secure: true });
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: "http://grpc.futureagi.com.:4317", headers: { ...KEYS } } as any)).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
  });
});

describe("one trailing dot is part of the Future AGI host in every decision", () => {
  test.each([
    ["https://api.futureagi.com.", "http", true], ["http://API.FUTUREAGI.COM./x", "http", true], ["grpc.futureagi.com.:443", "grpc", true],
    ["https://api.futureagi.com..", "http", false], ["https://api.futureagi.com.evil.example", "http", false], ["https://futureagi.com.example", "http", false],
  ] as const)("%s (%s)", (endpoint, transport, expected) => { expect(isFiEndpoint(endpoint, transport)).toBe(expected); });
  test("an https trailing-dot base selected by the environment is the FI route and gets the env keys", async () => {
    process.env.FI_BASE_URL = "https://api.futureagi.com.";
    await createSpanExporter({}).shutdown();
    await checkCustomEvalConfigExists("trailing-dot", [tag()]);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect((fetchSpy.mock.calls[0][1] as any).headers["x-api-key"]).toBe("env-key-synthetic-0002");
  });
});

describe("the deprecated HTTP exporter follows the same rule", () => {
  test("an http:// Future AGI endpoint with FI keys is refused", () => {
    expect(() => new HTTPSpanExporter({ endpoint: "http://api.futureagi.com/tracer/v1/traces" } as any)).toThrow();
    expect(() => new HTTPSpanExporter({ endpoint: "http://api.futureagi.com./tracer/v1/traces", headers: { ...KEYS } } as any)).toThrow();
  });
  test("https Future AGI and loopback are accepted", () => {
    new HTTPSpanExporter({ endpoint: "https://api.futureagi.com/tracer/v1/traces" } as any);
    new HTTPSpanExporter({ endpoint: "http://127.0.0.1:1/tracer/v1/traces" } as any);
  });
});

describe("gRPC channel credentials", () => {
  test("caller insecure credentials for a Future AGI target with keys are refused, never replaced with TLS", () => {
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: "grpc.futureagi.com:443", headers: { ...KEYS }, credentials: grpc.credentials.createInsecure() } as any)).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
  });
  test("a loopback target that the FI environment selects keeps the plaintext channel", () => {
    process.env.FI_GRPC_COLLECTOR_ENDPOINT = "localhost:4317";
    const exporter = createSpanExporter({ transport: Transport.GRPC, headers: { ...KEYS } } as any);
    expect(channel(exporter)).toEqual({ address: "localhost:4317", secure: false });
  });
});

describe("resolveTraceEndpoint", () => {
  test("returns no userinfo and logs no userinfo or query", () => {
    const logged: string[] = [];
    diag.setLogger({ error: (m: string) => logged.push(m), warn: (m: string) => logged.push(m), info: () => {}, debug: () => {}, verbose: () => {} } as any, DiagLogLevel.WARN);
    try {
      const url = resolveTraceEndpoint("https://user:pass-synthetic@collector.example/custom?token=query-synthetic#frag");
      expect(url).not.toMatch(/user|pass-synthetic/);
      resolveTraceEndpoint("not a url but user:pass-synthetic@host?token=query-synthetic");
    } finally { diag.disable(); }
    expect(logged.join("\n")).not.toMatch(/user|pass-synthetic|query-synthetic|token/);
  });
});
