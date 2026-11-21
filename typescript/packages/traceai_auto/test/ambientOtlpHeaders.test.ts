import { createServer, IncomingHttpHeaders, Server } from "node:http";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";

// Offline: capture server on 127.0.0.1 only; synthetic header values only.
const AMBIENT_AUTH = "Bearer SYNTHETIC-OTHER-BACKEND";
const ENV_CASES: Array<[string, string]> = [
  ["OTEL_EXPORTER_OTLP_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-all`],
  ["OTEL_EXPORTER_OTLP_TRACES_HEADERS", `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-traces`],
];
const BOTH: Record<string, string> = { OTEL_EXPORTER_OTLP_HEADERS: "x-synthetic-ambient-general=general,x-synthetic-ambient-marker=ambient-all", OTEL_EXPORTER_OTLP_TRACES_HEADERS: `authorization=${AMBIENT_AUTH.replace(" ", "%20")},x-synthetic-ambient-marker=ambient-traces` };
const AMBIENT_NAMES = ["authorization", "x-synthetic-ambient-marker", "x-synthetic-ambient-general"];
let server: Server, base: string, received: IncomingHttpHeaders[];
let userServer: Server, userBase: string, userReceived: IncomingHttpHeaders[];
async function listen(into: () => IncomingHttpHeaders[]): Promise<[Server, string]> {
  const sink = createServer((req, res) => { req.resume(); req.on("end", () => { into().push(req.headers); res.setHeader("content-type", "application/json"); res.end("{}"); }); });
  await new Promise<void>(done => sink.listen(0, "127.0.0.1", done));
  return [sink, `http://127.0.0.1:${(sink.address() as AddressInfo).port}`];
}
beforeAll(async () => {
  [server, base] = await listen(() => received);
  [userServer, userBase] = await listen(() => userReceived);
});
afterAll(async () => { for (const s of [server, userServer]) { s.closeAllConnections(); await new Promise<void>(done => s.close(() => done())); } });

const KEYS = ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL"];
let saved: Record<string, string | undefined>;
beforeEach(() => { received = []; userReceived = []; saved = Object.fromEntries(KEYS.map(k => [k, process.env[k]])); KEYS.forEach(k => delete process.env[k]); });
afterEach(() => { for (const k of KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

function runInit(env: Record<string, string>): Promise<{ stderr: string }> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", "ambient-headers.ts")], {
      cwd: join(__dirname, ".."), timeout: 60000,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, FI_TEST_BASE_URL: base, ...env },
    });
    let stderr = ""; child.stderr.on("data", c => { stderr += c; }); child.on("close", () => done({ stderr }));
  });
}
function expectFiOnly(headers: IncomingHttpHeaders) {
  expect(headers["x-api-key"]).toBe("synthetic-fi-key");
  expect(headers["x-secret-key"]).toBe("synthetic-fi-secret");
  for (const name of AMBIENT_NAMES) expect(headers[name]).toBeUndefined();
}
describe.each(ENV_CASES)("ambient %s with the auto init() path", (name, value) => {
  test("ambient headers are not forwarded; FI headers are", async () => {
    await runInit({ [name]: value });
    expect(received.length).toBeGreaterThan(0);
    for (const headers of received) expectFiOnly(headers);
  });
});
test("both ambient variables together are not forwarded through init()", async () => {
  await runInit(BOTH);
  expect(received.length).toBeGreaterThan(0);
  for (const headers of received) expectFiOnly(headers);
});

// The relocatable launcher (dist/agent, or TRACEAI_TEST_AGENT) bundles its own exporters.
const agentDir = process.env.TRACEAI_TEST_AGENT || join(__dirname, "..", "dist", "agent");
describe("ambient headers with the bundled --require launcher", () => {
  let dir: string;
  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "traceai-ambient-"));
    writeFileSync(join(dir, "app.cjs"), `(async () => { const r = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({model:'gpt-fixture',messages:[{role:'user',content:'hello'}]}) }); await r.text(); })();`);
  });
  afterAll(() => rmSync(dir, { recursive: true, force: true }));
  test.each([...ENV_CASES.map(([name, value]) => [name, { [name]: value }] as const), ["both", BOTH] as const])("%s", async (_label, ambient) => {
    expect(existsSync(join(agentDir, "preload.cjs"))).toBe(true);
    const env: Record<string, string> = { PATH: process.env.PATH!, HOME: process.env.HOME!, FI_API_KEY: "synthetic-fi-key", FI_SECRET_KEY: "synthetic-fi-secret", FI_BASE_URL: base, MOCK_URL: base, FI_INSTRUMENTATIONS: "wire", ...ambient };
    await new Promise<void>(done => {
      const child = spawn(process.execPath, ["--require", join(agentDir, "preload.cjs"), "app.cjs"], { cwd: dir, env, timeout: 30000 });
      child.on("close", () => done());
    });
    expect(received.length).toBeGreaterThan(0);
    for (const headers of received) for (const name of AMBIENT_NAMES) expect(headers[name]).toBeUndefined();
    expect(received.some(headers => headers["x-api-key"] === "synthetic-fi-key" && headers["x-secret-key"] === "synthetic-fi-secret")).toBe(true);
  }, 60000);
});

// The application's own exporter and a caller header named x-api-key: FI keys in the environment never join it.
describe("another collector with a caller x-api-key and FI keys in the environment", () => {
  const FI_ENV = { FI_API_KEY: "synthetic-env-key", FI_SECRET_KEY: "synthetic-env-secret" };
  function expectOwnKeyOnly(headers: IncomingHttpHeaders) {
    expect(headers["x-api-key"]).toBe("own-backend-key");
    expect(headers["x-secret-key"]).toBeUndefined();
    expect(JSON.stringify(headers)).not.toMatch(/synthetic-env-/);
  }
  test("init() with an application provider", async () => {
    await new Promise<string>(done => {
      const proc = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", "user-endpoint-headers.ts")], {
        cwd: join(__dirname, ".."), timeout: 60000,
        env: { PATH: process.env.PATH, HOME: process.env.HOME, USER_TEST_BASE_URL: userBase, FI_BASE_URL: base, ...FI_ENV },
      });
      let stderr = ""; proc.stderr.on("data", c => { stderr += c; }); proc.on("close", () => done(stderr));
    });
    expect(userReceived.length).toBeGreaterThan(0);
    for (const headers of userReceived) expectOwnKeyOnly(headers);
  });
  test("the --require launcher next to the application's own exporter", async () => {
    const dir = mkdtempSync(join(tmpdir(), "traceai-user-endpoint-"));
    try {
      const exporterPath = require.resolve("@opentelemetry/exporter-trace-otlp-http"), basePath = require.resolve("@opentelemetry/sdk-trace-base"), apiPath = require.resolve("@opentelemetry/api");
      writeFileSync(join(dir, "app.cjs"), `
        const { trace } = require(${JSON.stringify(apiPath)});
        const { BasicTracerProvider, SimpleSpanProcessor } = require(${JSON.stringify(basePath)});
        const { OTLPTraceExporter } = require(${JSON.stringify(exporterPath)});
        (async () => {
          const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: process.env.USER_TEST_BASE_URL + '/v1/traces', headers: { 'x-api-key': 'own-backend-key' } }))] });
          trace.setGlobalTracerProvider(provider);
          trace.getTracer('user-endpoint').startSpan('one').end();
          await provider.forceFlush(); await provider.shutdown();
        })();`);
      const agent = process.env.TRACEAI_TEST_AGENT || join(__dirname, "..", "dist", "agent");
      await new Promise<void>(done => {
        const child = spawn(process.execPath, ["--require", join(agent, "preload.cjs"), "app.cjs"], { cwd: dir, timeout: 30000,
          env: { PATH: process.env.PATH!, HOME: process.env.HOME!, USER_TEST_BASE_URL: userBase, FI_BASE_URL: base, FI_INSTRUMENTATIONS: "wire", ...FI_ENV } });
        child.on("close", () => done());
      });
      expect(userReceived.length).toBeGreaterThan(0);
      for (const headers of userReceived) expectOwnKeyOnly(headers);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  }, 60000);
});
