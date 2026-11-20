import * as grpc from "@grpc/grpc-js";
import "../fi-route";
import { createSpanExporter, Transport, registerSecret } from "../index";

/**
 * An exporter for another collector merges OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_TRACES_HEADERS into what it
 * sends. When those carry a Future AGI key header, or any known secret, the exporter is not created: nothing can be sent.
 * They are read when the exporter is constructed, at the point the OTLP exporter reads them; changing them afterwards is not
 * supported.
 */
const NAMES = ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_GRPC_COLLECTOR_ENDPOINT",
  "FI_ALLOW_INSECURE_HTTP", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"];
let saved: Record<string, string | undefined> = {};
beforeEach(() => { saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; } });
afterEach(() => { for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

const OWN_HTTP = "http://collector.example:4318", OWN_GRPC = "collector.example:4317";
const http = (extra: Record<string, unknown> = {}) => createSpanExporter({ endpoint: OWN_HTTP, allowInsecureHttp: true, ...extra } as any);
const rpc = (extra: Record<string, unknown> = {}) => createSpanExporter({ transport: Transport.GRPC, endpoint: OWN_GRPC, credentials: grpc.credentials.createInsecure(), ...extra } as any);
function refused(build: () => unknown): string {
  let message = "";
  try { build(); } catch (error) { message = String((error as Error).message); }
  return message;
}
const PAIR = "x-api-key=ambient-key-synthetic-0001,x-secret-key=ambient-secret-synthetic-0001";

describe.each(["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"])("a Future AGI key pair in %s", name => {
  test("is refused for an HTTP exporter for another collector", () => {
    process.env[name] = PAIR;
    const message = refused(() => http());
    expect(message).not.toBe("");
    expect(message).toMatch(/OTEL_EXPORTER_OTLP/);
    expect(message).not.toMatch(/synthetic|collector\.example/);
  });
  test("is refused for a gRPC exporter for another collector", () => {
    process.env[name] = PAIR;
    expect(refused(() => rpc())).toMatch(/OTEL_EXPORTER_OTLP/);
  });
  test("is refused with a proxy in the environment too", () => {
    process.env[name] = PAIR; process.env.HTTP_PROXY = "http://proxy.example:3128"; process.env.http_proxy = "http://proxy.example:3128";
    expect(refused(() => http())).not.toBe("");
    expect(refused(() => rpc())).not.toBe("");
  });
  test("is refused in any header-name case and when only one half is there", () => {
    process.env[name] = "X-Api-Key=ambient-key-synthetic-0001";
    expect(refused(() => http())).not.toBe("");
    process.env[name] = "X-SECRET-KEY=ambient-secret-synthetic-0001";
    expect(refused(() => rpc())).not.toBe("");
  });
  test("is refused when the other half comes from the headers option, the metadata or the environment", () => {
    process.env[name] = "x-secret-key=ambient-secret-synthetic-0001";
    expect(refused(() => http({ headers: { "x-api-key": "own-key-synthetic-0002" } }))).not.toBe("");
    const metadata = new grpc.Metadata(); metadata.set("x-api-key", "own-key-synthetic-0002");
    expect(refused(() => rpc({ metadata }))).not.toBe("");
    process.env.FI_API_KEY = "env-key-synthetic-0003";
    expect(refused(() => http())).not.toBe("");
  });
});

describe("a known secret in an ambient header", () => {
  test("the Future AGI key values, under an unrelated header name, are refused", () => {
    process.env.FI_API_KEY = "env-key-synthetic-0003"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0003";
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-trace-token=prefix-env-secret-synthetic-0003-suffix";
    expect(refused(() => http())).not.toBe("");
    expect(refused(() => rpc())).not.toBe("");
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-trace-token=env-key-synthetic-0003";
    expect(refused(() => http())).not.toBe("");
  });
  test("a registered known secret is refused too", () => {
    registerSecret("registered-secret-synthetic-0004");
    process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS = "x-other=registered-secret-synthetic-0004";
    expect(refused(() => http())).not.toBe("");
    expect(refused(() => rpc())).not.toBe("");
  });
  test("a value that only looks similar is not refused", () => {
    process.env.FI_API_KEY = "env-key-synthetic-0003"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0003";
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "x-trace-token=env-key-synthetic-0004";
    expect(refused(() => http())).toBe("");
  });
});

describe("unchanged behaviour", () => {
  test("an unrelated ambient header on an exporter for another collector is kept", async () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = "authorization=Bearer%20other-backend-token";
    await http().shutdown();
    const exporter = rpc();
    await exporter.shutdown();
  });
  test("no ambient headers: nothing changes", async () => {
    await http().shutdown();
    await rpc().shutdown();
  });
  test("a Future AGI bound exporter is not affected by the ambient variables (they are kept out of its requests)", async () => {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = PAIR;
    const exporter = createSpanExporter({ endpoint: "https://api.futureagi.com", headers: { "x-api-key": "explicit-key-synthetic-0005", "x-secret-key": "explicit-secret-synthetic-0005" } });
    await exporter.shutdown();
  });
});
