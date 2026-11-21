import * as grpc from "@grpc/grpc-js";
import http from "node:http";
import { AddressInfo } from "node:net";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter, Transport, getRegisteredSecrets } from "../index";
import { FI_ROUTE_FACTORY } from "../fi-route";

const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE", "FI_API_KEY", "FI_SECRET_KEY", "FI_GRPC_COLLECTOR_ENDPOINT", "FI_GRPC_URL", "FI_BASE_URL", "FI_TRACES_URL", "FI_COLLECTOR_ENDPOINT"];
let saved: Record<string, string | undefined> = {};
beforeEach(() => { saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; } });
afterEach(() => { for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

/** What the installed OTLP gRPC exporter dials and with which channel credentials. */
function channel(exporter: unknown): { address: string; secure: boolean } {
  const parameters = (exporter as any)._delegate._transport._parameters;
  return { address: parameters.address, secure: parameters.credentials()._isSecure() };
}
const grpcExporter = (options: Record<string, unknown>) => createSpanExporter({ transport: Transport.GRPC, headers: { ...KEYS }, ...options } as any);

describe("a Future AGI bound gRPC exporter uses TLS", () => {
  test("default FI target with OTEL_EXPORTER_OTLP_INSECURE=true", () => {
    process.env.OTEL_EXPORTER_OTLP_INSECURE = "true";
    expect(channel(grpcExporter({}))).toEqual({ address: "grpc.futureagi.com", secure: true });
  });
  test("default FI target with OTEL_EXPORTER_OTLP_TRACES_INSECURE=true", () => {
    process.env.OTEL_EXPORTER_OTLP_TRACES_INSECURE = "true";
    expect(channel(grpcExporter({}))).toEqual({ address: "grpc.futureagi.com", secure: true });
  });
  test("an http:// Future AGI target is refused, never upgraded to TLS", () => {
    expect(() => grpcExporter({ endpoint: "http://grpc.futureagi.com:4317" })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
    process.env.OTEL_EXPORTER_OTLP_INSECURE = "true";
    expect(() => grpcExporter({ endpoint: "grpc.futureagi.com:4317" })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
  });
  test("an http:// target selected by the environment is a plaintext request: the same plaintext rule applies, as for any other target", () => {
    process.env.FI_GRPC_COLLECTOR_ENDPOINT = "http://collector.internal.example:4317";
    expect(() => grpcExporter({})).toThrow(/FI_ALLOW_INSECURE_HTTP/);
    process.env.FI_GRPC_COLLECTOR_ENDPOINT = "http://grpc.futureagi.com:4317";
    expect(() => grpcExporter({})).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
    process.env.FI_GRPC_COLLECTOR_ENDPOINT = "collector.internal.example:4317";
    expect(channel(grpcExporter({})).secure).toBe(true);
  });
  test("loopback FI bound targets keep the plaintext channel", () => {
    process.env.FI_GRPC_COLLECTOR_ENDPOINT = "127.0.0.1:4317";
    expect(channel(grpcExporter({}))).toEqual({ address: "127.0.0.1:4317", secure: false });
    expect(channel(grpcExporter({ endpoint: "localhost:4317" })).secure).toBe(false);
  });
});

describe("a gRPC exporter for another collector keeps the exporter's own choice", () => {
  const other = (endpoint: string, headers: Record<string, string> = {}) => createSpanExporter({ transport: Transport.GRPC, endpoint, headers } as any);
  test.each([
    ["0.0.0.0:4317", false],
    ["host.docker.internal:4317", false],
    ["http://host.docker.internal:4317", true],
    ["http://0.0.0.0:4317", true],
  ])("%s", (endpoint, plaintext) => {
    expect(channel(other(endpoint)).secure).toBe(!plaintext);
  });
  test("OTEL_EXPORTER_OTLP_INSECURE applies to it", () => {
    process.env.OTEL_EXPORTER_OTLP_INSECURE = "true";
    expect(channel(other("host.docker.internal:4317")).secure).toBe(false);
  });
  test("caller credentials are used", () => {
    const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: "collector.example:4317", credentials: grpc.credentials.createInsecure() } as any);
    expect(channel(exporter).secure).toBe(false);
  });
});

describe("an http:// Future AGI HTTP endpoint is not used with FI keys", () => {
  test.each(["http://api.futureagi.com", "http://api.futureagi.com/tracer/v1/traces", "http://eu.futureagi.com:8080"])("%s with an explicit pair is refused", endpoint => {
    let message = "";
    try { createSpanExporter({ endpoint, headers: { ...KEYS } }); } catch (error) { message = String((error as Error).message); }
    expect(message).not.toBe("");
    expect(message).not.toMatch(/synthetic|futureagi/);
  });
  test("an http:// Future AGI endpoint selected by the environment is refused with env keys", () => {
    process.env.FI_BASE_URL = "http://api.futureagi.com"; process.env.FI_API_KEY = "env-key-synthetic"; process.env.FI_SECRET_KEY = "env-secret-synthetic";
    expect(() => createSpanExporter({})).toThrow();
  });
  test("https Future AGI, loopback and own collectors are unaffected", async () => {
    for (const endpoint of ["https://api.futureagi.com", "http://127.0.0.1:1", "http://localhost:1", "https://collector.example:4318"]) {
      const exporter = createSpanExporter({ endpoint, headers: { ...KEYS } });
      await exporter.shutdown();
    }
  });
  test("an http:// Future AGI endpoint without FI keys is a plain collector", async () => {
    await createSpanExporter({ endpoint: "http://api.futureagi.com" }).shutdown();
  });
});

async function sink() {
  const received: http.IncomingHttpHeaders[] = [];
  const server = http.createServer((req, res) => { req.resume(); req.on("end", () => { received.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  return { received, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, close: () => new Promise<void>(done => { server.closeAllConnections(); server.close(() => done()); }) };
}

describe("the FI route factory reads its headers once", () => {
  test("a getter-based headers object cannot show one pair to the check and another to the export", async () => {
    const collector = await sink();
    process.env.FI_BASE_URL = collector.url; process.env.FI_API_KEY = "env-key-synthetic-001"; process.env.FI_SECRET_KEY = "env-secret-synthetic-001";
    let reads = 0;
    const headers = {} as Record<string, string>;
    for (const [name, value] of Object.entries(KEYS)) Object.defineProperty(headers, name, { enumerable: true, get: () => (reads++ < 2 ? value : "later-value-synthetic") });
    const factory = (globalThis as any)[FI_ROUTE_FACTORY];
    const exporter = factory({ headers });
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    provider.getTracer("t").startSpan("one").end();
    await provider.forceFlush(); await provider.shutdown(); await collector.close();
    expect(collector.received).toHaveLength(1);
    expect(collector.received[0]["x-api-key"]).toBe(KEYS["x-api-key"]);
    expect(collector.received[0]["x-secret-key"]).toBe(KEYS["x-secret-key"]);
  });
  test("non-string header values are not a pair", () => {
    const factory = (globalThis as any)[FI_ROUTE_FACTORY];
    expect(() => factory({ endpoint: "http://127.0.0.1:1", headers: { "x-api-key": { toString: () => "synthetic-object-key" }, "x-secret-key": 12345678 } })).toThrow();
  });
});

describe("caller gRPC metadata", () => {
  test("every value of a repeated credential entry is a known secret", async () => {
    const metadata = new grpc.Metadata();
    metadata.add("x-api-key", "repeated-credential-value-one"); metadata.add("x-api-key", "repeated-credential-value-two");
    await createSpanExporter({ transport: Transport.GRPC, endpoint: "collector.example:4317", metadata } as any).shutdown();
    const known = getRegisteredSecrets();
    expect(known).toContain("repeated-credential-value-one");
    expect(known).toContain("repeated-credential-value-two");
  });
});
