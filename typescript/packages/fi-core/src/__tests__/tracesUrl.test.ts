// FI_TRACES_URL sends span exports to a trace collector separate from the
// platform API in FI_BASE_URL. Synthetic local servers only.
import * as http from "http";
import { AddressInfo } from "net";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter } from "../otel";

const saved = { ...process.env };
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  process.env.FI_API_KEY = "synthetic-key";
  process.env.FI_SECRET_KEY = "synthetic-secret";
});
afterEach(() => { process.env = { ...saved }; });

function server(): Promise<{ url: string; paths: string[]; close: () => Promise<void> }> {
  const paths: string[] = [];
  const s = http.createServer((req, res) => {
    paths.push(req.url ?? "");
    req.resume();
    req.on("end", () => { res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); });
  });
  return new Promise((resolve) => s.listen(0, "127.0.0.1", () => {
    const { port } = s.address() as AddressInfo;
    resolve({ url: `http://127.0.0.1:${port}`, paths, close: () => new Promise((r) => s.close(() => r())) });
  }));
}

async function exportOneSpan(): Promise<void> {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(createSpanExporter())] });
  provider.getTracer("traces-url").startSpan("export-check").end();
  await provider.forceFlush();
  await provider.shutdown();
}

test("exports go to FI_TRACES_URL, not FI_BASE_URL", async () => {
  const api = await server();
  const collector = await server();
  try {
    process.env.FI_BASE_URL = api.url;
    process.env.FI_TRACES_URL = `${collector.url}/`;
    await exportOneSpan();
    expect(collector.paths).toEqual(["/tracer/v1/traces"]);
    expect(api.paths).toEqual([]);
  } finally {
    await api.close();
    await collector.close();
  }
});

test.each([undefined, "", "   "])("FI_TRACES_URL %p falls back to FI_BASE_URL", async (value) => {
  const api = await server();
  try {
    process.env.FI_BASE_URL = api.url;
    if (value !== undefined) process.env.FI_TRACES_URL = value;
    await exportOneSpan();
    expect(api.paths).toEqual(["/tracer/v1/traces"]);
  } finally {
    await api.close();
  }
});
