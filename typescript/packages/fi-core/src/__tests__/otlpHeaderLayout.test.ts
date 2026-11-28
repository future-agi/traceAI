import { createServer, Server } from "node:http";
import type { AddressInfo } from "node:net";
import { createSpanExporter, Transport } from "../otel";

// An exporter whose header configuration cannot be pinned is refused at construction, with a
// message that carries no header names or values, and nothing is sent.
let layout: "ok" | "headers-not-a-function" | "unaccounted-header" | "metadata-not-metadata" = "ok";
jest.mock("@opentelemetry/otlp-exporter-base/node-http", () => {
  const actual = jest.requireActual("@opentelemetry/otlp-exporter-base/node-http");
  return { ...actual, convertLegacyHttpOptions: (...args: unknown[]) => {
    const resolved = actual.convertLegacyHttpOptions(...args);
    if (layout === "headers-not-a-function") return { ...resolved, headers: { "x-synthetic-static": "value" } };
    if (layout === "unaccounted-header") return { ...resolved, headers: () => ({ ...resolved.headers(), "x-synthetic-unknown-default": "synthetic-secret-value" }) };
    return resolved;
  } };
});
jest.mock("@opentelemetry/otlp-grpc-exporter-base", () => {
  const actual = jest.requireActual("@opentelemetry/otlp-grpc-exporter-base");
  return { ...actual, convertLegacyOtlpGrpcOptions: (...args: unknown[]) => {
    const resolved = actual.convertLegacyOtlpGrpcOptions(...args);
    return layout === "metadata-not-metadata" ? { ...resolved, metadata: () => ({ synthetic: "plain-object" }) } : resolved;
  } };
});

const FI = { "x-api-key": "synthetic-fi-key", "x-secret-key": "synthetic-fi-secret" };
let server: Server, base: string, hits: number;
beforeAll(async () => {
  server = createServer((req, res) => { req.resume(); req.on("end", () => { hits++; res.end("{}"); }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); });
beforeEach(() => { hits = 0; layout = "ok"; delete process.env.FI_BASE_URL; delete process.env.FI_GRPC_COLLECTOR_ENDPOINT; });
afterEach(() => { delete process.env.FI_BASE_URL; delete process.env.FI_GRPC_COLLECTOR_ENDPOINT; });

const refusal = (fn: () => unknown) => {
  let thrown: Error | undefined;
  try { fn(); } catch (error) { thrown = error as Error; }
  expect(thrown?.message).toMatch(/^Unsupported OTLP exporter layout: /);
  expect(thrown?.message).not.toMatch(/synthetic|x-api-key|x-secret-key|authorization|x-synthetic/i);
};

test("an HTTP exporter whose headers are not a provider function is refused", () => {
  layout = "headers-not-a-function"; process.env.FI_BASE_URL = base;
  refusal(() => createSpanExporter({ headers: FI }));
  expect(hits).toBe(0);
});
test("an HTTP exporter that adds a header we cannot account for is refused", () => {
  layout = "unaccounted-header"; process.env.FI_BASE_URL = base;
  refusal(() => createSpanExporter({ headers: FI }));
  expect(hits).toBe(0);
});
test("a gRPC exporter whose metadata is not gRPC metadata is refused", () => {
  layout = "metadata-not-metadata"; process.env.FI_GRPC_COLLECTOR_ENDPOINT = "127.0.0.1:1";
  refusal(() => createSpanExporter({ headers: FI, transport: Transport.GRPC }));
});
test("an exporter for another collector is not subject to the layout check", () => {
  layout = "headers-not-a-function";
  expect(() => createSpanExporter({ endpoint: base })).not.toThrow();
});

// Shipped isolation does not look at the environment: the same exporter is classified the same way
// whatever variables are set, and anything that is not a pinnable OTLP exporter is refused.
describe("an FI-bound exporter is classified the same way in every environment", () => {
  const { isolateHttpExporterHeaders, isolateGrpcExporterHeaders } = jest.requireActual("../otlp-header-isolation");
  const withEnv = (env: Record<string, string | undefined>, run: () => void) => {
    const saved = Object.fromEntries(Object.keys(env).map(k => [k, process.env[k]]));
    for (const [k, v] of Object.entries(env)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
    try { run(); } finally { for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; } }
  };
  const ENVS: Array<Record<string, string | undefined>> = [{ JEST_WORKER_ID: undefined }, { JEST_WORKER_ID: "1" }, { JEST_WORKER_ID: "1", NODE_ENV: "test", CI: "true" }];
  const odd = [["a plain object with no delegate", {}], ["an object from an unknown base class", new (class Unknown { export() {} shutdown() {} forceFlush() {} })()], ["null", null], ["a number", 7], ["a delegate shape that cannot be pinned", { _delegate: {} }]] as Array<[string, unknown]>;
  test.each(odd)("%s is refused under every environment (HTTP and gRPC)", (_label, exporter) => {
    for (const env of ENVS) withEnv(env, () => {
      refusal(() => isolateHttpExporterHeaders(exporter, { url: base }, FI));
      refusal(() => isolateGrpcExporterHeaders(exporter, { url: "127.0.0.1:1" }));
    });
  });
  test("a real exporter is isolated the same way under every environment", () => {
    process.env.FI_BASE_URL = base;
    for (const env of ENVS) withEnv(env, () => expect(() => createSpanExporter({ headers: FI })).not.toThrow());
  });
  test("shipped source has no environment-dependent skip", () => {
    const { readdirSync, readFileSync } = require("node:fs"), { join } = require("node:path");
    const dir = join(__dirname, "..");
    for (const name of readdirSync(dir).filter((n: string) => n.endsWith(".ts"))) expect(readFileSync(join(dir, name), "utf8")).not.toMatch(/JEST_WORKER_ID|process\.env\.JEST/);
  });
});
