import { createServer, IncomingHttpHeaders, Server } from "node:http";
import type { AddressInfo } from "node:net";
import * as grpc from "@grpc/grpc-js";
import { FITracerProvider, Transport } from "../otel";

// Offline: a capture server bound to 127.0.0.1 only; synthetic header values only.
const AMBIENT_AUTH = "Bearer SYNTHETIC-OTHER-BACKEND";
const ENV_CASES: Array<[string, string]> = [
  ["OTEL_EXPORTER_OTLP_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-all`],
  ["OTEL_EXPORTER_OTLP_TRACES_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-traces`],
];
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

async function exportOneSpan(transport?: Transport, endpoint = base): Promise<void> {
  const provider = new FITracerProvider({ projectName: "ambient-headers", endpoint, transport, headers: FI, batch: false, verbose: false } as any);
  provider.getTracer("ambient-headers").startSpan("one").end();
  await provider.forceFlush(); await provider.shutdown();
}

// Known failure while ambient headers reach the FI exporter; remove .skip once fixed.
describe.each(ENV_CASES)("ambient %s with the HTTP exporter", (name, value) => {
  const run = process.env.AMBIENT_HEADERS_EXPECT_FIXED ? test : test.skip;
  run("ambient headers are not forwarded; FI headers are (ambient OTLP headers reach the FI exporter; fix pending)", async () => {
    process.env[name] = value;
    await exportOneSpan();
    expect(received).toHaveLength(1);
    expect(received[0]["x-api-key"]).toBe(FI["x-api-key"]);
    expect(received[0]["x-secret-key"]).toBe(FI["x-secret-key"]);
    expect(received[0]["authorization"]).toBeUndefined();
    expect(received[0]["x-synthetic-ambient-marker"]).toBeUndefined();
  });
});

describe.each(ENV_CASES)("ambient %s with the gRPC exporter", (name, value) => {
  const run = process.env.AMBIENT_HEADERS_EXPECT_FIXED ? test : test.skip;
  run("ambient headers are not forwarded; FI headers are (ambient OTLP headers reach the FI exporter; fix pending)", async () => {
    const metadata: grpc.Metadata[] = [];
    const grpcServer = new grpc.Server();
    const id = (b: Buffer) => b;
    grpcServer.addService({ Export: { path: "/opentelemetry.proto.collector.trace.v1.TraceService/Export", requestStream: false, responseStream: false,
      requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } } as any,
      { Export: (call: any, cb: any) => { metadata.push(call.metadata); cb(null, Buffer.alloc(0)); } } as any);
    const port = await new Promise<number>((ok, bad) => grpcServer.bindAsync("127.0.0.1:0", grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
    try {
      process.env[name] = value;
      await exportOneSpan(Transport.GRPC, `127.0.0.1:${port}`);
    } finally { grpcServer.forceShutdown(); }
    expect(metadata).toHaveLength(1);
    expect(metadata[0].get("x-api-key")).toEqual([FI["x-api-key"]]);
    expect(metadata[0].get("x-secret-key")).toEqual([FI["x-secret-key"]]);
    expect(metadata[0].get("authorization")).toEqual([]);
    expect(metadata[0].get("x-synthetic-ambient-marker")).toEqual([]);
  });
});
