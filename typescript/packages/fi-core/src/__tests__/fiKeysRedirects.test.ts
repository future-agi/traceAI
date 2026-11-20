import http from "node:http";
import { AddressInfo } from "node:net";
import { diag, DiagLogLevel } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import "../fi-route";
import { checkCustomEvalConfigExists, checkCustomEvalTemplateExists, HTTPSpanExporter } from "../otel";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

/**
 * A request that carries Future AGI keys does not follow a redirect: a 3xx answer is an error, nothing further is
 * sent, and the refusal names no value.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_TRACES_URL", "FI_COLLECTOR_ENDPOINT", "FI_ALLOW_INSECURE_HTTP"];
let saved: Record<string, string | undefined> = {};
const logged: string[] = [];
beforeEach(() => {
  saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; }
  process.env.FI_API_KEY = "env-key-synthetic-0002"; process.env.FI_SECRET_KEY = "env-secret-synthetic-0002";
  logged.length = 0;
  diag.setLogger({ error: m => logged.push(m), warn: m => logged.push(m), info: () => {}, debug: () => {}, verbose: () => {} }, { logLevel: DiagLogLevel.WARN, suppressOverrideMessage: true });
});
afterEach(() => { diag.disable(); for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
  mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });

interface Servers { first: string; secondRequests: http.IncomingHttpHeaders[]; firstRequests: number; close(): Promise<void> }
async function servers(status = 302): Promise<Servers> {
  const result = { secondRequests: [] as http.IncomingHttpHeaders[], firstRequests: 0 } as Servers;
  const second = http.createServer((req, res) => { req.resume(); req.on("end", () => { result.secondRequests.push(req.headers); res.setHeader("content-type", "application/json"); res.end("{\"result\":{\"exists\":true}}"); }); });
  await new Promise<void>(done => second.listen(0, "127.0.0.1", done));
  const secondUrl = `http://127.0.0.1:${(second.address() as AddressInfo).port}/redirected`;
  const first = http.createServer((req, res) => { req.resume(); req.on("end", () => { result.firstRequests++; res.statusCode = status; res.setHeader("location", secondUrl); res.end(); }); });
  await new Promise<void>(done => first.listen(0, "127.0.0.1", done));
  result.first = `http://127.0.0.1:${(first.address() as AddressInfo).port}`;
  result.close = () => new Promise<void>(done => { first.closeAllConnections(); second.closeAllConnections(); first.close(() => second.close(() => done())); });
  return result;
}
const noValues = (): void => { for (const line of logged) expect(line).not.toMatch(/synthetic|127\.0\.0\.1:\d+\/redirected/); };

describe.each([301, 302, 303, 307, 308])("an approved target answering %i", status => {
  test("the custom eval config check does not follow it", async () => {
    const s = await servers(status);
    try {
      process.env.FI_BASE_URL = s.first;
      expect(await checkCustomEvalConfigExists("p", [tag()])).toBe(false);
      expect(s.firstRequests).toBe(1);
      expect(s.secondRequests).toHaveLength(0);
      noValues();
    } finally { await s.close(); }
  });
  test("the custom eval template check does not follow it", async () => {
    const s = await servers(status);
    try {
      process.env.FI_BASE_URL = s.first;
      const result = await checkCustomEvalTemplateExists("template");
      expect(result.result?.isUserEvalTemplate).toBe(false);
      expect(s.firstRequests).toBe(1);
      expect(s.secondRequests).toHaveLength(0);
      noValues();
    } finally { await s.close(); }
  });
  test("the deprecated JSON exporter does not follow it and reports the failure", async () => {
    const s = await servers(status);
    try {
      const exporter = new HTTPSpanExporter({ endpoint: `${s.first}/tracer/v1/traces`, headers: { ...KEYS } } as any);
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      provider.getTracer("t").startSpan("one").end();
      await provider.forceFlush(); await provider.shutdown();
      expect(s.firstRequests).toBe(1);
      expect(s.secondRequests).toHaveLength(0);
      expect(logged.some(line => /redirect/i.test(line))).toBe(true);
      noValues();
    } finally { await s.close(); }
  });
});

test("an ordinary answer is still used", async () => {
  const s = await servers(200);
  try {
    process.env.FI_BASE_URL = s.first;
    expect(await checkCustomEvalConfigExists("p", [tag()])).toBe(false); // the first server answers 200 with no body: not an existing config
    expect(s.firstRequests).toBe(1);
  } finally { await s.close(); }
});
