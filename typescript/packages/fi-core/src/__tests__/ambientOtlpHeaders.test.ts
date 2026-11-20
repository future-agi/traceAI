import { createServer, IncomingHttpHeaders, Server } from "node:http";
import type { AddressInfo } from "node:net";
import * as grpc from "@grpc/grpc-js";
import { FITracerProvider, register, Transport } from "../otel";

// Offline: a capture server bound to 127.0.0.1 only; synthetic header values only.
const AMBIENT_AUTH = "Bearer SYNTHETIC-OTHER-BACKEND";
const ENV_CASES: Array<[string, string]> = [
  ["OTEL_EXPORTER_OTLP_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-all`],
  ["OTEL_EXPORTER_OTLP_TRACES_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-traces`],
];
const BOTH = { OTEL_EXPORTER_OTLP_HEADERS: "x-synthetic-ambient-general=general,x-synthetic-ambient-marker=ambient-all", OTEL_EXPORTER_OTLP_TRACES_HEADERS: `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-traces` };
const FI = { "x-api-key": "synthetic-fi-key", "x-secret-key": "synthetic-fi-secret" };

let server: Server, base: string, received: IncomingHttpHeaders[];
beforeAll(async () => {
  server = createServer((req, res) => {
    req.resume(); req.on("end", () => { received.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); });

const KEYS = ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "FI_API_KEY", "FI_SECRET_KEY"];
let saved: Record<string, string | undefined>;
beforeEach(() => { received = []; saved = Object.fromEntries(KEYS.map(k => [k, process.env[k]])); KEYS.forEach(k => delete process.env[k]); });
afterEach(() => { for (const k of KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

async function exportOneSpan(transport?: Transport, endpoint = base, headers: Record<string, string> | null = FI): Promise<void> {
  const provider = new FITracerProvider({ projectName: "ambient-headers", endpoint, transport, headers: headers ?? undefined, batch: false, verbose: false } as any);
  provider.getTracer("ambient-headers").startSpan("one").end();
  await provider.forceFlush(); await provider.shutdown();
}

describe.each(ENV_CASES)("ambient %s with the HTTP exporter", (name, value) => {
  test("ambient headers are not forwarded; FI headers are", async () => {
    process.env[name] = value;
    await exportOneSpan();
    expect(received).toHaveLength(1);
    expect(received[0]["x-api-key"]).toBe(FI["x-api-key"]);
    expect(received[0]["x-secret-key"]).toBe(FI["x-secret-key"]);
    expect(received[0]["authorization"]).toBeUndefined();
    expect(received[0]["x-synthetic-ambient-marker"]).toBeUndefined();
  });
});

async function withGrpcSink(run: (endpoint: string) => Promise<void>): Promise<grpc.Metadata[]> {
  const metadata: grpc.Metadata[] = [];
  const grpcServer = new grpc.Server();
  const id = (b: Buffer) => b;
  grpcServer.addService({ Export: { path: "/opentelemetry.proto.collector.trace.v1.TraceService/Export", requestStream: false, responseStream: false,
    requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } } as any,
    { Export: (call: any, cb: any) => { metadata.push(call.metadata); cb(null, Buffer.alloc(0)); } } as any);
  const port = await new Promise<number>((ok, bad) => grpcServer.bindAsync("127.0.0.1:0", grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
  try { await run(`127.0.0.1:${port}`); } finally { grpcServer.forceShutdown(); }
  return metadata;
}

describe.each(ENV_CASES)("ambient %s with the gRPC exporter", (name, value) => {
  test("ambient headers are not forwarded; FI headers are", async () => {
    process.env[name] = value;
    const metadata = await withGrpcSink(endpoint => exportOneSpan(Transport.GRPC, endpoint));
    expect(metadata).toHaveLength(1);
    expect(metadata[0].get("x-api-key")).toEqual([FI["x-api-key"]]);
    expect(metadata[0].get("x-secret-key")).toEqual([FI["x-secret-key"]]);
    expect(metadata[0].get("authorization")).toEqual([]);
    expect(metadata[0].get("x-synthetic-ambient-marker")).toEqual([]);
  });
});

describe("both ambient variables together, and explicit caller headers", () => {
  const caller = { ...FI, "x-caller-custom": "from-the-caller" };
  test("HTTP: neither variable reaches FI; FI and caller headers do", async () => {
    Object.assign(process.env, BOTH);
    await exportOneSpan(undefined, base, caller);
    expect(received).toHaveLength(1);
    expect(received[0]["x-api-key"]).toBe(FI["x-api-key"]);
    expect(received[0]["x-secret-key"]).toBe(FI["x-secret-key"]);
    expect(received[0]["x-caller-custom"]).toBe("from-the-caller");
    for (const name of ["authorization", "x-synthetic-ambient-marker", "x-synthetic-ambient-general"]) expect(received[0][name]).toBeUndefined();
    expect(received[0]["content-type"]).toBe("application/json");
    expect(String(received[0]["user-agent"])).toContain("OTel-OTLP-Exporter-JavaScript");
  });
  test("gRPC: neither variable reaches FI; FI and caller headers do", async () => {
    Object.assign(process.env, BOTH);
    const metadata = await withGrpcSink(endpoint => exportOneSpan(Transport.GRPC, endpoint, caller));
    expect(metadata).toHaveLength(1);
    expect(metadata[0].get("x-api-key")).toEqual([FI["x-api-key"]]);
    expect(metadata[0].get("x-secret-key")).toEqual([FI["x-secret-key"]]);
    expect(metadata[0].get("x-caller-custom")).toEqual(["from-the-caller"]);
    for (const name of ["authorization", "x-synthetic-ambient-marker", "x-synthetic-ambient-general"]) expect(metadata[0].get(name)).toEqual([]);
  });
  test("an ambient header with the same name as a caller header loses to the caller's", async () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-caller-custom=ambient-value";
    await exportOneSpan(undefined, base, caller);
    expect(received[0]["x-caller-custom"]).toBe("from-the-caller");
  });
  test("FI keys from the environment still authenticate an exporter that targets the FI endpoint", async () => {
    process.env.FI_API_KEY = "synthetic-env-key"; process.env.FI_SECRET_KEY = "synthetic-env-secret"; process.env.FI_BASE_URL = base;
    Object.assign(process.env, BOTH);
    try { await exportOneSpan(undefined, undefined, null); } finally { delete process.env.FI_BASE_URL; }
    expect(received).toHaveLength(1);
    expect(received[0]["x-api-key"]).toBe("synthetic-env-key");
    expect(received[0]["x-secret-key"]).toBe("synthetic-env-secret");
    expect(received[0]["authorization"]).toBeUndefined();
  });
});

// Reverse direction: an exporter for the application's own collector never carries FI keys, and
// keeps the OTLP headers configured for it.
describe("an exporter pointed at another collector", () => {
  const OWN = { OTEL_EXPORTER_OTLP_HEADERS: `authorization=${AMBIENT_AUTH.replace(" ", "%20")}` };
  beforeEach(() => { process.env.FI_API_KEY = "synthetic-env-key"; process.env.FI_SECRET_KEY = "synthetic-env-secret"; });
  test("HTTP: gets no FI keys from the environment, and its own OTLP headers", async () => {
    Object.assign(process.env, OWN);
    await exportOneSpan(undefined, base, null);
    expect(received).toHaveLength(1);
    expect(received[0]["x-api-key"]).toBeUndefined();
    expect(received[0]["x-secret-key"]).toBeUndefined();
    expect(received[0]["authorization"]).toBe(AMBIENT_AUTH);
  });
  test("HTTP: only the headers the caller passes are added", async () => {
    await exportOneSpan(undefined, base, { "x-own-backend": "token" });
    expect(received[0]["x-own-backend"]).toBe("token");
    expect(received[0]["x-api-key"]).toBeUndefined();
    expect(received[0]["x-secret-key"]).toBeUndefined();
  });
  test("gRPC: gets no FI keys from the environment, and its own OTLP headers", async () => {
    Object.assign(process.env, OWN);
    const metadata = await withGrpcSink(endpoint => exportOneSpan(Transport.GRPC, endpoint, null));
    expect(metadata).toHaveLength(1);
    expect(metadata[0].get("x-api-key")).toEqual([]);
    expect(metadata[0].get("x-secret-key")).toEqual([]);
    expect(metadata[0].get("authorization")).toEqual([AMBIENT_AUTH]);
  });
  test("register(): the provider it builds adds no FI keys either", async () => {
    const provider = register({ projectName: "ambient-headers", endpoint: base, setGlobalTracerProvider: false, batch: false, verbose: false } as any);
    provider.getTracer("ambient-headers").startSpan("one").end();
    await provider.forceFlush(); await provider.shutdown();
    expect(received).toHaveLength(1);
    expect(received[0]["x-api-key"]).toBeUndefined();
    expect(received[0]["x-secret-key"]).toBeUndefined();
  });
});
