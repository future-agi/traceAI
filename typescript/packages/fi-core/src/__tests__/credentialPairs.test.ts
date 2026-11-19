import { diag } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { createSpanExporter, FITracerProvider } from "../otel";

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
const lastOptions = () => (OTLPTraceExporter as jest.Mock).mock.calls.at(-1)![0];
test.each(["X-API-Key", "X-Secret-Key"])("S7-pre core explicit %s half is ignored atomically, preserving non-FI headers", half => {
  process.env.FI_API_KEY = "environment-private-key"; process.env.FI_SECRET_KEY = "environment-private-secret";
  createSpanExporter({ endpoint: "https://collector.invalid", headers: { [half]: "explicit-private-half", custom: "keep" } });
  expect(lastOptions().headers).toEqual({ custom: "keep", "x-api-key": "environment-private-key", "x-secret-key": "environment-private-secret" });
  expect(warn).toHaveBeenCalledTimes(1); expect(JSON.stringify(warn.mock.calls)).not.toContain("private");
});
test("S7-pre core complete explicit pair wins; custom/empty auth does not inherit FI credentials", () => {
  process.env.FI_API_KEY = "environment-private-key"; process.env.FI_SECRET_KEY = "environment-private-secret";
  const variants: Record<string, string>[] = [{ "x-api-key": "code-private-key", "x-secret-key": "code-private-secret" }, { Authorization: "Bearer custom-auth" }, {}];
  for (const headers of variants) {
    createSpanExporter({ endpoint: "https://collector.invalid", headers }); expect(lastOptions().headers).toEqual(headers);
  }
  expect(warn).not.toHaveBeenCalled();
});
test("S7-pre core does not combine opposite halves across explicit headers and environment", () => {
  process.env.FI_SECRET_KEY = "environment-private-half";
  createSpanExporter({ endpoint: "https://collector.invalid", headers: { "x-api-key": "explicit-private-half" } });
  expect(lastOptions().headers).toEqual({}); expect(warn).toHaveBeenCalledTimes(2);
  expect(JSON.stringify(warn.mock.calls)).toContain("FI_SECRET_KEY"); expect(JSON.stringify(warn.mock.calls)).toContain("FI_API_KEY");
  expect(JSON.stringify(warn.mock.calls)).not.toContain("private");
});
test("S7-pre core provider resolves an incomplete environment once without constructing a mixed pair", async () => {
  process.env.FI_API_KEY = "environment-private-half";
  const provider = new FITracerProvider({ endpoint: "https://collector.invalid" });
  expect(lastOptions().headers).toEqual({}); expect(warn).toHaveBeenCalledTimes(1);
  await provider.shutdown();
});
