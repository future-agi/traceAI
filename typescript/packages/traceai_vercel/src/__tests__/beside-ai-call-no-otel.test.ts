import { spawn } from "node:child_process";
import { createServer, Server } from "node:http";
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { AddressInfo } from "node:net";

// An application with no OpenTelemetry of its own makes one AI SDK call and, while that
// call is open, one plain fetch to the same chat route. The plain fetch has no AI
// integration of its own, so its wire span is its only record. These tests pass only
// when Future AGI receives both: the AI SDK call's model span, and the plain fetch.
const root = resolve(__dirname, "../..");
const auto = resolve(root, "../traceai_auto/src/register.ts");
// The question each request carries. Neither text contains the other.
const SDK_PROMPT = "question sent through the AI SDK", PLAIN_PROMPT = "question sent with a plain fetch";
const BOTH = [PLAIN_PROMPT, SDK_PROMPT].sort();
const completion = { id: "chat-fixture", model: "fixture", choices: [{ index: 0, message: { role: "assistant", content: "hello world" }, finish_reason: "stop" }], usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 } };
const sse = [
  { id: "chat-fixture", model: "fixture", choices: [{ index: 0, delta: { role: "assistant", content: "hello " }, finish_reason: null }] },
  { id: "chat-fixture", model: "fixture", choices: [{ index: 0, delta: { content: "world" }, finish_reason: "stop" }], usage: completion.usage },
].map(event => `data: ${JSON.stringify(event)}\n\n`).join("") + "data: [DONE]\n\n";
// What the provider received: the question of each chat request, in arrival order.
let server: Server, url: string, collected: any[], received: string[];
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
      if (req.url === "/tracer/v1/traces") {
        collected.push(JSON.parse(body));
        res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); return;
      }
      if (req.url !== "/v1/chat/completions") {
        received.push(`unexpected ${req.method} ${req.url}`);
        res.writeHead(400, { "content-type": "application/json" }); res.end('{"error":{"message":"unexpected fixture request"}}'); return;
      }
      const data = JSON.parse(body);
      received.push(String(data.messages?.[0]?.content));
      if (data.stream) { res.writeHead(200, { "content-type": "text/event-stream" }); res.end(sse); }
      else { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(completion)); }
    });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => {
  server.closeAllConnections();
  await new Promise<void>(done => server.close(() => done()));
});
beforeEach(() => { collected = []; received = []; });

// Runs the application in a child process with a real `ai` package of the given major,
// without the agent (baseline) or with it (traced). The child has no OpenTelemetry
// setup: no-otel-preload.cjs only lets it load the TypeScript sources.
async function child(major: number, method: string, traced: boolean): Promise<any> {
  const scratch = mkdtempSync(join(root, ".traceai-vercel-beside-"));
  try {
    const modules = join(scratch, "node_modules");
    mkdirSync(join(modules, "@traceai/vercel"), { recursive: true });
    // AI SDK aliases still have their genuine package name `ai`. Use ordinary
    // Node resolution so the real instrumentation hooks see that name.
    const alias = major === 5 ? "ai-v5" : major === 7 ? "ai-v7" : "ai";
    symlinkSync(dirname(realpathSync(require.resolve(`${alias}/package.json`))), join(modules, "ai"), "dir");
    // Only the source entry is redirected; no instrumentation or resolver is mocked.
    writeFileSync(join(modules, "@traceai/vercel/package.json"), JSON.stringify({
      name: "@traceai/vercel", main: join(root, "src/index.ts"),
    }));
    const app = join(scratch, "app.cjs");
    writeFileSync(app, readFileSync(join(__dirname, "beside-ai-call-no-otel-app.cjs")));
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      // No inherited keys, preload, privacy override, collector, or TS loader.
      if (/^(?:FI_|OTEL_|TS_NODE_)/.test(key) || /KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH/i.test(key) || key === "NODE_OPTIONS") delete env[key];
    }
    Object.assign(env, { NODE_OPTIONS: "", FI_API_KEY: "mock-fi-api-key", FI_SECRET_KEY: "mock-fi-secret-key",
      FI_BASE_URL: url, FI_INSTRUMENTATIONS: "vercel,wire", FI_PROJECT_NAME: "vercel-no-otel-test",
      MOCK_URL: url, SDK_PROMPT, PLAIN_PROMPT });
    const args = ["--no-deprecation", "-r", join(__dirname, "no-otel-preload.cjs"), ...(traced ? ["-r", auto] : []), app, String(major), method, traced ? "traced" : "baseline"];
    const output = await new Promise<string>((done, reject) => {
      const proc = spawn(process.execPath, args, { cwd: scratch, env, stdio: ["ignore", "pipe", "pipe"] });
      let stdout = "", stderr = "";
      const timer = setTimeout(() => { proc.kill("SIGKILL"); reject(new Error(`Child timed out: ${stderr}\n${stdout}`)); }, 20000);
      proc.stdout.on("data", chunk => { stdout += chunk; });
      proc.stderr.on("data", chunk => { stderr += chunk; });
      proc.on("error", error => { clearTimeout(timer); reject(error); });
      proc.on("close", code => {
        clearTimeout(timer);
        if (code !== 0) reject(new Error(`Child exited ${code}: ${stderr}\n${stdout}`));
        else done(stdout.trim());
      });
    });
    return JSON.parse(output.split("\n").at(-1)!);
  } finally { rmSync(scratch, { recursive: true, force: true }); }
}

describe.each([5, 6, 7])("AI SDK v%i in an application with no OpenTelemetry of its own", major => {
  test.each([
    ["in the same tick as a generateText call", "generateText"],
    ["while the application reads a streamText stream", "streamText"],
  ])("a plain fetch AI request sent %s is not lost, and the call keeps its model span", async (_when, method) => {
    // First the application without the agent: both of its requests reach the provider
    // and both answers come back. A failure up to here is in the fixture, not a lost span.
    const baseline = await child(major, method, false);
    expect(baseline.application).toEqual({ text: "hello world", plain: "hello world", providerCalls: 1 });
    expect([...received].sort()).toEqual(BOTH);
    expect(collected).toHaveLength(0);
    received = [];
    // Then with the agent. The application behaves the same, and the provider gets both
    // requests, each once.
    const actual = await child(major, method, true);
    expect(actual.application).toEqual(baseline.application);
    expect([...received].sort()).toEqual(BOTH);
    // The case under test: the agent is on with the AI SDK integration loaded, and the
    // application still has no context manager.
    expect({ active: actual.active, vercel: actual.vercel, contextManager: actual.contextManager })
      .toEqual({ active: true, vercel: "instrumented", contextManager: "NoopContextManager" });

    // What Future AGI received, shown when one of the two checks below fails.
    const spans = exportedSpans();
    const got = spans.map(span => `${span.name} (${attrs(span)["traceai.capture.layer"]}, ${attrs(span)["gen_ai.span.kind"]})`);
    const carries = (span: any, text: string) => JSON.stringify(span).includes(text);
    // The AI SDK call arrives as its model span: an LLM span from the integration, not
    // from wire capture, with the question the application gave the SDK.
    const model = spans.filter(span => attrs(span)["traceai.capture.layer"] !== "wire" && attrs(span)["gen_ai.span.kind"] === "LLM" && carries(span, SDK_PROMPT));
    expect({ got, modelSpan: model.length > 0 }).toEqual({ got, modelSpan: true });
    // The plain fetch arrives as a wire span with its own question. It is found by that
    // text, so the SDK's own request, sent a second time, could not stand in for it.
    const plain = spans.filter(span => attrs(span)["traceai.capture.layer"] === "wire" && carries(span, PLAIN_PROMPT));
    expect({ got, plainFetchWireSpan: plain.length > 0 }).toEqual({ got, plainFetchWireSpan: true });
  }, 60000);
});
