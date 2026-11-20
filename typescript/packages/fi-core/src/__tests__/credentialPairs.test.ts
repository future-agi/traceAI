import { diag } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { createSpanExporter, FITracerProvider } from "../otel";

// These tests replace the exporter class with a plain stand-in that has no OTLP delegate. Shipped
// isolation classifies every exporter the same way whatever the environment, so the stand-in is
// exempted here, in the test, by stubbing the isolation step.
jest.mock("../otlp-header-isolation", () => ({ ...(jest.requireActual("../otlp-header-isolation") as object), isolateHttpExporterHeaders: jest.fn(), isolateGrpcExporterHeaders: jest.fn() }));
jest.mock("@opentelemetry/exporter-trace-otlp-http", () => ({ OTLPTraceExporter: jest.fn(() => ({ export: jest.fn(), shutdown: jest.fn(() => Promise.resolve()) })) }));
let savedKey: string | undefined, savedSecret: string | undefined;
let warn: jest.SpyInstance;
beforeEach(() => {
  savedKey = process.env.FI_API_KEY; savedSecret = process.env.FI_SECRET_KEY;
  delete process.env.FI_API_KEY; delete process.env.FI_SECRET_KEY;
  jest.clearAllMocks(); warn = jest.spyOn(diag, "warn").mockImplementation(() => {});
});
afterEach(() => {
  if (savedKey === undefined) delete process.env.FI_API_KEY; else process.env.FI_API_KEY = savedKey;
  if (savedSecret === undefined) delete process.env.FI_SECRET_KEY; else process.env.FI_SECRET_KEY = savedSecret;
  warn.mockRestore();
});
// An exporter for another collector sends the caller's headers exactly as given: nothing is filled in
// from the FI environment and no warning is raised, whatever header names the caller passed.
test.each([["x-api-key only", { "x-api-key": "own-key" }], ["x-secret-key only", { "x-secret-key": "own-secret" }], ["both", { "x-api-key": "own-key", "x-secret-key": "own-secret" }], ["other names", { custom: "keep" }]] as Array<[string, Record<string, string>]>)(
  "another collector: %s is sent exactly as given", (_label, headers) => {
    process.env.FI_API_KEY = "environment-private-key"; process.env.FI_SECRET_KEY = "environment-private-secret";
    createSpanExporter({ endpoint: "https://collector.invalid", headers });
    expect(lastOptions().headers).toEqual(headers);
    expect(warn).not.toHaveBeenCalled();
  });
const lastOptions = () => (OTLPTraceExporter as jest.Mock).mock.calls.at(-1)![0];
test.each(["X-API-Key", "X-Secret-Key"])("S7-pre core explicit %s half is ignored atomically, preserving non-FI headers", half => {
  process.env.FI_API_KEY = "environment-private-key"; process.env.FI_SECRET_KEY = "environment-private-secret";
  createSpanExporter({ endpoint: "https://api.futureagi.com", headers: { [half]: "explicit-private-half", custom: "keep" } });
  expect(lastOptions().headers).toEqual({ custom: "keep", "x-api-key": "environment-private-key", "x-secret-key": "environment-private-secret" });
  expect(warn).toHaveBeenCalledTimes(1); expect(JSON.stringify(warn.mock.calls)).not.toContain("private");
});
test("S7-pre core complete explicit pair wins; custom/empty auth does not inherit FI credentials", () => {
  process.env.FI_API_KEY = "environment-private-key"; process.env.FI_SECRET_KEY = "environment-private-secret";
  const variants: Record<string, string>[] = [{ "x-api-key": "code-private-key", "x-secret-key": "code-private-secret" }, { Authorization: "Bearer custom-auth" }, {}];
  for (const headers of variants) {
    createSpanExporter({ endpoint: "https://api.futureagi.com", headers }); expect(lastOptions().headers).toEqual(headers);
  }
  expect(warn).not.toHaveBeenCalled();
});
test("S7-pre core does not combine opposite halves across explicit headers and environment", () => {
  process.env.FI_SECRET_KEY = "environment-private-half";
  createSpanExporter({ endpoint: "https://api.futureagi.com", headers: { "x-api-key": "explicit-private-half" } });
  expect(lastOptions().headers).toEqual({}); expect(warn).toHaveBeenCalledTimes(2);
  expect(JSON.stringify(warn.mock.calls)).toContain("FI_SECRET_KEY"); expect(JSON.stringify(warn.mock.calls)).toContain("FI_API_KEY");
  expect(JSON.stringify(warn.mock.calls)).not.toContain("private");
});
test("S7-pre core provider resolves an incomplete environment once without constructing a mixed pair", async () => {
  process.env.FI_API_KEY = "environment-private-half";
  const provider = new FITracerProvider({ endpoint: "https://api.futureagi.com" });
  expect(lastOptions().headers).toEqual({}); expect(warn).toHaveBeenCalledTimes(1);
  await provider.shutdown();
});
