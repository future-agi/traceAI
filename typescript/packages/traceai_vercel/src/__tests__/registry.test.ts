import { spawn } from "node:child_process";
import { createServer, Server } from "node:http";
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { AddressInfo } from "node:net";

const root = resolve(__dirname, "../..");
const auto = resolve(root, "../traceai_auto/src/register.ts");
let server: Server, url: string, collected: any[], requests: number;
const attrs = (span: any): Record<string, any> => Object.fromEntries((span.attributes ?? []).map((item: any) => [
  item.key, item.value.stringValue ?? item.value.intValue ?? item.value.boolValue ?? item.value.arrayValue,
]));
const exportedSpans = () => collected.flatMap(batch => batch.resourceSpans ?? [])
  .flatMap(resource => resource.scopeSpans ?? []).flatMap(scope => scope.spans ?? []);

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString();
      res.setHeader("content-type", "application/json");
      if (req.url === "/tracer/v1/traces") {
        collected.push(JSON.parse(body)); res.end("{}"); return;
      }
      requests++;
      if (req.url !== "/v1/chat/completions" || !req.headers.authorization?.startsWith("Bearer ")) {
        res.writeHead(400); res.end('{"error":{"message":"unexpected fixture request"}}'); return;
      }
      const secret = req.headers.authorization.slice(7);
      res.writeHead(401);
      res.end(JSON.stringify({ error: { message: "Invalid credential " + secret } }));
    });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => {
  server.closeAllConnections();
  await new Promise<void>(done => server.close(() => done()));
});
beforeEach(() => { collected = []; requests = 0; });

async function child(major: number, method: string, traced: boolean, forward = false): Promise<any> {
  const scratch = mkdtempSync(join(root, ".traceai-vercel-s5-"));
  try {
    const modules = join(scratch, "node_modules");
    mkdirSync(join(modules, "@traceai/vercel"), { recursive: true });
    // AI SDK aliases still have their genuine package name `ai`. Use ordinary
    // Node resolution so the real CJS/ESM instrumentation hooks see that name.
    const alias = major === 5 ? "ai-v5" : major === 7 ? "ai-v7" : "ai";
    symlinkSync(dirname(realpathSync(require.resolve(`${alias}/package.json`))), join(modules, "ai"), "dir");
    // Only the source entry is redirected; no instrumentation/resolver is mocked.
    writeFileSync(join(modules, "@traceai/vercel/package.json"), JSON.stringify({
      name: "@traceai/vercel", main: join(root, "src/index.ts"),
    }));
    const app = join(scratch, "app.cjs");
    writeFileSync(app, readFileSync(join(__dirname, "registry-app.cjs")));
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      // No inherited keys, preload, privacy override, collector, or TS loader.
      if (/^(?:FI_|OTEL_|TS_NODE_)/.test(key) || /KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH/i.test(key) || key === "NODE_OPTIONS") delete env[key];
    }
    Object.assign(env, { NODE_OPTIONS: "", FI_API_KEY: "synthetic-fi-api-private-key", FI_SECRET_KEY: "synthetic-fi-private-secret",
      FI_BASE_URL: url, FI_INSTRUMENTATIONS: "vercel,wire", FI_PROJECT_NAME: "vercel-s5-test",
      MOCK_URL: url, OTEL_SDK_DISABLED: "false" });
    // The switch that also sends the application's own spans to Future AGI.
    if (forward) env.FI_FORWARD_APP_SPANS = "true";
    const args = ["--no-deprecation", "-r", join(__dirname, "registry-preload.cjs"), ...(traced ? ["-r", auto] : []), app, String(major), method, traced ? "traced" : "baseline"];
    // Serial child runtimes stay inside the suite's heavy.sh process-tree budget;
    // acquiring a second heavy slot here could deadlock concurrent guarded suites.
    const output = await new Promise<string>((done, reject) => {
      const proc = spawn(process.execPath, args, { cwd: scratch, env, stdio: ["ignore", "pipe", "pipe"] });
      let stdout = "", stderr = "";
      const timer = setTimeout(() => { proc.kill("SIGKILL"); reject(new Error("S5 child timed out")); }, 20000);
      proc.stdout.on("data", chunk => { stdout += chunk; });
      proc.stderr.on("data", chunk => { stderr += chunk; });
      proc.on("error", error => { clearTimeout(timer); reject(error); });
      proc.on("close", code => {
        clearTimeout(timer);
        if (code !== 0) reject(new Error(`S5 child exited ${code}: ${stderr}\n${stdout}`));
        else if (stderr.trim()) reject(new Error(`S5 child diagnostics: ${stderr}`));
        else done(stdout.trim());
      });
    });
    return JSON.parse(output);
  } finally { rmSync(scratch, { recursive: true, force: true }); }
}

// Spans the application creates on its own provider: one enclosing each AI call, one
// unrelated span after it, and one inside the second call's provider transport.
const APPLICATION_SPANS = ["enclosing application", "later unrelated application", "sdk-native client"];
const names = (spans: any[]): string[] => spans.map(span => span.name).sort();

// Runs the application without and then with the agent. On every route the application
// behaves as without the agent, the provider sees each call once, and nothing Future AGI
// receives holds the provider credential or the Future AGI keys.
async function run(major: number, method: string, forward: boolean): Promise<{ baseline: any; actual: any; spans: any[] }> {
  const baseline = await child(major, method, false);
  expect(collected).toHaveLength(0);
  const actual = await child(major, method, true, forward);
  expect(actual.application).toEqual(baseline.application);
  expect(actual.live).toBe(0);
  expect(requests).toBe(4); // Once each, direct and intermediate-native, baseline and agent.
  const output = JSON.stringify(collected);
  for (const result of actual.application) {
    expect(output).not.toContain(result.secret);
    expect(output).not.toContain(encodeURIComponent(result.secret));
  }
  expect(output).not.toContain("synthetic-fi-api-private-key");
  expect(output).not.toContain("synthetic-fi-private-secret");
  return { baseline, actual, spans: exportedSpans() };
}

// The AI SDK's model spans: one per call, failed, with the input captured. The wire
// layer does not send the same call a second time.
function expectModelSpans(spans: any[]): void {
  expect(spans.filter(span => attrs(span)["traceai.capture.layer"] === "wire").map(span => span.name)).toEqual([]);
  const models = spans.filter(span => attrs(span)["gen_ai.span.kind"] === "LLM");
  expect(models).toHaveLength(2);
  models.forEach(span => expect(span.status.code).toBe(2));
  // Capture is ON by default. Passing by dropping all telemetry is not enough.
  expect(models.every(span => JSON.stringify(span.attributes).includes("ordinary visible input"))).toBe(true);
}

describe.each([5, 6, 7])("zero-code S5 AI SDK v%i", major => {
  test.each(["generateText", "streamText"])("%s provider 401 scrubs the covered AI spans; enclosing and unrelated application spans stay with the application", async method => {
    const { baseline, actual, spans } = await run(major, method, false);
    // Future AGI receives only AI spans. None of the application's own spans reach it:
    // not the span enclosing the call, the unrelated one after it, or the one the
    // application creates inside the call.
    const application = new Set(actual.raw.map((span: any) => span.id));
    expect(spans.filter(span => application.has(span.spanId)).map(span => span.name)).toEqual([]);
    expect(names(spans).filter(name => APPLICATION_SPANS.includes(name))).toEqual([]);
    // They stay with the application's own exporter, as without the agent.
    expect(names(actual.raw)).toEqual(names(baseline.raw));
    expect(actual.raw.filter((span: any) => span.name === "enclosing application")).toHaveLength(2);
    expect(actual.raw.filter((span: any) => span.name === "later unrelated application")).toHaveLength(2);
    expectModelSpans(spans);
  }, 45000);

  test.each(["generateText", "streamText"])("%s with FI_FORWARD_APP_SPANS on, provider 401 scrubs covered, enclosing and unrelated spans through the real exporter", async method => {
    const { actual, spans } = await run(major, method, true);
    // The switch sends every span the application's provider ends, AI and application alike.
    expect(spans).toHaveLength(actual.raw.length);
    expect(new Set(spans.map(span => span.spanId))).toEqual(new Set(actual.raw.map((span: any) => span.id)));
    expectModelSpans(spans);
    const parents = spans.filter(span => span.name === "enclosing application");
    const unrelated = spans.filter(span => span.name === "later unrelated application");
    expect(parents).toHaveLength(2);
    expect(unrelated).toHaveLength(2);
    parents.forEach(span => {
      expect(attrs(span).safe).toBe("ordinary visible input");
      expect(attrs(span).echoed).toBeDefined();
      expect(span.status.message).toContain("<redacted>");
      expect(span.events.some((event: any) => event.name === "exception")).toBe(true);
      expect(span.links).toHaveLength(1);
    });
    unrelated.forEach(span => expect(attrs(span).safe).toBe("ordinary visible input"));
  }, 45000);
});
