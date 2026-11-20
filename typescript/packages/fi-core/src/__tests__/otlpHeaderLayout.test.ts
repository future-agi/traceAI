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

// No early return: an FI-bound exporter that cannot be classified is refused, never passed through.
describe("an FI-bound exporter is classified inside the refusal block", () => {
  const { isolateHttpExporterHeaders, isolateGrpcExporterHeaders } = jest.requireActual("../otlp-header-isolation");
  const withoutJest = (run: () => void) => {
    const saved = process.env.JEST_WORKER_ID; delete process.env.JEST_WORKER_ID;
    try { run(); } finally { if (saved !== undefined) process.env.JEST_WORKER_ID = saved; }
  };
  const odd = [["a plain object with no delegate", {}], ["an object from an unknown base class", new (class Unknown { export() {} shutdown() {} forceFlush() {} })()], ["null", null], ["a number", 7]] as Array<[string, unknown]>;
  test.each(odd)("outside a test runner, %s is refused (HTTP and gRPC)", (_label, exporter) => withoutJest(() => {
    refusal(() => isolateHttpExporterHeaders(exporter, { url: base }, FI));
    refusal(() => isolateGrpcExporterHeaders(exporter, { url: "127.0.0.1:1" }));
  }));
  test.each(odd)("under jest, only a mock object may skip: %s", (_label, exporter) => {
    expect(() => isolateHttpExporterHeaders(exporter, { url: base }, FI)).not.toThrow();
  });
  test("an object with a delegate shape that cannot be pinned is refused even under jest", () => {
    refusal(() => isolateHttpExporterHeaders({ _delegate: {} }, { url: base }, FI));
  });
});
