import { createServer, IncomingHttpHeaders, Server } from "node:http";
import type { AddressInfo } from "node:net";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as grpc from "@grpc/grpc-js";
import { checkCustomEvalConfigExists, checkCustomEvalTemplateExists, createSpanExporter, FITracerProvider, GRPCSpanExporter, register, Transport } from "../otel";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

// Offline: capture servers bound to 127.0.0.1 only; synthetic credentials only.
const KEY = "synthetic-fi-key", SECRET = "synthetic-fi-secret";
const FI_KEYS = ["x-api-key", "x-secret-key"];
const ENV_NAMES = ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_GRPC_COLLECTOR_ENDPOINT", "FI_GRPC_URL", "FI_TRACES_URL", "FI_COLLECTOR_ENDPOINT"];
let saved: Record<string, string | undefined>;
beforeEach(() => {
  saved = Object.fromEntries(ENV_NAMES.map(k => [k, process.env[k]])); ENV_NAMES.forEach(k => delete process.env[k]);
  process.env.FI_API_KEY = KEY; process.env.FI_SECRET_KEY = SECRET;
});
afterEach(() => { for (const k of ENV_NAMES) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

interface HttpSink { base: string; seen: IncomingHttpHeaders[]; close(): Promise<void> }
async function httpSink(): Promise<HttpSink> {
  const seen: IncomingHttpHeaders[] = [];
  const server: Server = createServer((req, res) => { req.resume(); req.on("end", () => { seen.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  return { base: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, seen, close: () => { server.closeAllConnections(); return new Promise<void>(done => server.close(() => done())); } };
}
async function withGrpcSink(run: (endpoint: string) => Promise<void>): Promise<grpc.Metadata[]> {
  const metadata: grpc.Metadata[] = [];
  const server = new grpc.Server(), id = (b: Buffer) => b;
  server.addService({ Export: { path: "/opentelemetry.proto.collector.trace.v1.TraceService/Export", requestStream: false, responseStream: false,
    requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } } as any,
    { Export: (call: any, cb: any) => { metadata.push(call.metadata); cb(null, Buffer.alloc(0)); } } as any);
  const port = await new Promise<number>((ok, bad) => server.bindAsync("127.0.0.1:0", grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
  try { await run(`127.0.0.1:${port}`); } finally { server.forceShutdown(); }
  return metadata;
}
async function exportWith(exporter: any): Promise<void> {
  const { SimpleSpanProcessor } = await import("@opentelemetry/sdk-trace-base");
  const provider = new FITracerProvider({ projectName: "exporter-target", spanProcessors: [new SimpleSpanProcessor(exporter)] } as any);
  provider.getTracer("exporter-target").startSpan("one").end();
  await provider.forceFlush(); await provider.shutdown();
}
async function exportViaProvider(config: object): Promise<void> {
  const provider = new FITracerProvider({ projectName: "exporter-target", batch: false, ...config } as any);
  provider.getTracer("exporter-target").startSpan("one").end();
  await provider.forceFlush(); await provider.shutdown();
}
const noFiKeys = (headers: IncomingHttpHeaders | grpc.Metadata): void => {
  for (const name of FI_KEYS) {
    if (headers instanceof grpc.Metadata) expect(headers.get(name)).toEqual([]); else expect(headers[name]).toBeUndefined();
  }
};

describe("a caller url does not redirect the exporter's Future AGI classification (gRPC)", () => {
  test("createSpanExporter: url names a collector that is not Future AGI, so it gets no FI keys", async () => {
    const seen = await withGrpcSink(url => exportWith(createSpanExporter({ transport: Transport.GRPC, url } as any)));
    expect(seen).toHaveLength(1); noFiKeys(seen[0]);
  });
  test("FITracerProvider: url names a collector that is not Future AGI, so it gets no FI keys", async () => {
    const seen = await withGrpcSink(url => exportViaProvider({ transport: Transport.GRPC, url }));
    expect(seen).toHaveLength(1); noFiKeys(seen[0]);
  });
  test("the exported GRPCSpanExporter: url alone names the collector", async () => {
    const seen = await withGrpcSink(url => exportWith(new (GRPCSpanExporter as any)({ url })));
    expect(seen).toHaveLength(1); noFiKeys(seen[0]);
  });
  test("an endpoint and a url that name different targets are refused, naming neither", async () => {
    let thrown: unknown;
    const seen = await withGrpcSink(async url => {
      try { createSpanExporter({ transport: Transport.GRPC, endpoint: "grpc.futureagi.com", url } as any); } catch (error) { thrown = error; }
      try { new (GRPCSpanExporter as any)({ endpoint: "grpc.futureagi.com", url }); } catch (error) { thrown = thrown ?? error; }
    });
    expect(thrown).toBeInstanceOf(Error);
    expect(String((thrown as Error).message)).not.toMatch(/futureagi|127\.0\.0\.1/);
    expect(seen).toHaveLength(0);
  });
  test("caller metadata is added to the headers option rather than replacing them", async () => {
    const metadata = new grpc.Metadata(); metadata.set("x-caller-custom", "from-the-caller");
    const seen = await withGrpcSink(endpoint => { process.env.FI_GRPC_COLLECTOR_ENDPOINT = endpoint; return exportWith(createSpanExporter({ transport: Transport.GRPC, headers: { "x-api-key": KEY, "x-secret-key": SECRET }, metadata } as any)); });
    expect(seen).toHaveLength(1);
    expect(seen[0].get("x-api-key")).toEqual([KEY]); expect(seen[0].get("x-secret-key")).toEqual([SECRET]);
    expect(seen[0].get("x-caller-custom")).toEqual(["from-the-caller"]);
  });
  test("caller credentials do not change how a Future AGI bound exporter connects", async () => {
    const seen = await withGrpcSink(endpoint => { process.env.FI_GRPC_COLLECTOR_ENDPOINT = endpoint; return exportWith(createSpanExporter({ transport: Transport.GRPC, credentials: grpc.credentials.createSsl() } as any)); });
    expect(seen).toHaveLength(1);
    expect(seen[0].get("x-api-key")).toEqual([KEY]);
  });
  test("a header with the same name as an ambient one wins over it", async () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-caller-custom=ambient-value";
    const seen = await withGrpcSink(endpoint => { process.env.FI_GRPC_COLLECTOR_ENDPOINT = endpoint; return exportWith(createSpanExporter({ transport: Transport.GRPC, headers: { "x-api-key": KEY, "x-secret-key": SECRET, "x-caller-custom": "from-the-caller" } })); });
    expect(seen).toHaveLength(1);
    expect(seen[0].get("x-caller-custom")).toEqual(["from-the-caller"]);
    expect(seen[0].get("x-api-key")).toEqual([KEY]);
  });
});

describe("a caller url on the HTTP exporter", () => {
  let sink: HttpSink;
  beforeEach(async () => { sink = await httpSink(); });
  afterEach(() => sink.close());
  test("createSpanExporter: url names the collector, which gets no FI keys", async () => {
    await exportWith(createSpanExporter({ url: `${sink.base}/v1/traces` } as any));
    expect(sink.seen).toHaveLength(1); noFiKeys(sink.seen[0]);
  });
  test("FITracerProvider: url names the collector, which gets no FI keys", async () => {
    await exportViaProvider({ url: `${sink.base}/v1/traces` });
    expect(sink.seen).toHaveLength(1); noFiKeys(sink.seen[0]);
  });
  test("an endpoint and a url that name different targets are refused", async () => {
    expect(() => createSpanExporter({ endpoint: "https://api.futureagi.com", url: `${sink.base}/v1/traces` } as any)).toThrow();
    expect(sink.seen).toHaveLength(0);
  });
});

describe("the custom eval checks send FI keys only to a Future AGI base", () => {
  let other: HttpSink, fi: HttpSink;
  beforeEach(async () => { other = await httpSink(); fi = await httpSink(); });
  afterEach(async () => { await other.close(); await fi.close(); });
  const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
    mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
  const settle = () => new Promise<void>(done => setTimeout(done, 400));

  test("register with another endpoint and evalTags sends no FI keys to that endpoint", async () => {
    process.env.FI_BASE_URL = fi.base;
    const provider = register({ projectName: "eval-target", endpoint: other.base, evalTags: [tag() as any], setGlobalTracerProvider: false });
    await settle(); await provider.shutdown();
    for (const headers of other.seen) noFiKeys(headers);
    expect(fi.seen.length).toBeGreaterThan(0);
    expect(fi.seen[0]["x-api-key"]).toBe(KEY);
  });
  test("checkCustomEvalConfigExists given another endpoint checks the Future AGI base instead", async () => {
    process.env.FI_BASE_URL = fi.base;
    await checkCustomEvalConfigExists("eval-target", [tag()], other.base);
    for (const headers of other.seen) noFiKeys(headers);
    expect(fi.seen).toHaveLength(1); expect(fi.seen[0]["x-api-key"]).toBe(KEY);
  });
  test("checkCustomEvalTemplateExists given another endpoint checks the Future AGI base instead", async () => {
    process.env.FI_BASE_URL = fi.base;
    await checkCustomEvalTemplateExists("template", false, other.base);
    for (const headers of other.seen) noFiKeys(headers);
    expect(fi.seen).toHaveLength(1); expect(fi.seen[0]["x-api-key"]).toBe(KEY);
  });
  test("an endpoint that is the Future AGI base is still used, with the FI keys", async () => {
    process.env.FI_BASE_URL = fi.base;
    await checkCustomEvalConfigExists("eval-target", [tag()], fi.base);
    expect(fi.seen).toHaveLength(1); expect(fi.seen[0]["x-api-key"]).toBe(KEY);
  });
});

describe("the package exports", () => {
  test("the Future AGI route factory is not a public subpath", () => {
    const manifest = JSON.parse(readFileSync(join(__dirname, "../../package.json"), "utf8"));
    expect(Object.keys(manifest.exports)).not.toContain("./fi-route");
    expect(JSON.stringify(manifest.typesVersions)).not.toContain("fi-route");
  });
});
