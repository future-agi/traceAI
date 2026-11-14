import { spawn } from "node:child_process";
import { createServer, Server } from "node:http";
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { AddressInfo } from "node:net";

// An application with no OpenTelemetry of its own starts one Groq call and, in the same
// tick, one plain fetch to an OpenAI-compatible chat route. Groq's integration patches the
// SDK itself, so the agent has no call scope around it. The plain fetch has no AI
// integration of its own, so its wire span is its only record. This test passes only when
// Future AGI receives both: the Groq call's model span, and the plain fetch.
const root = resolve(__dirname, "../..");
const auto = resolve(root, "../traceai_auto/src/register.ts");
// The question each request carries. Neither text contains the other.
const SDK_PROMPT = "question sent through the Groq SDK", PLAIN_PROMPT = "question sent with a plain fetch";
const BOTH = [PLAIN_PROMPT, SDK_PROMPT].sort();
const completion = { id: "chat-fixture", object: "chat.completion", created: 0, model: "fixture", choices: [{ index: 0, message: { role: "assistant", content: "hello world" }, finish_reason: "stop" }], usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 } };
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
      // The plain fetch uses /v1/chat/completions; the Groq SDK uses /openai/v1/chat/completions.
      if (!["/v1/chat/completions", "/openai/v1/chat/completions"].includes(req.url ?? "")) {
        received.push(`unexpected ${req.method} ${req.url}`);
        res.writeHead(400, { "content-type": "application/json" }); res.end('{"error":{"message":"unexpected fixture request"}}'); return;
      }
      const data = JSON.parse(body);
      received.push(String(data.messages?.[0]?.content));
      res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(completion));
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

// Runs the application in a child process with the real `groq-sdk` package, without the
// agent (baseline) or with it (traced). The child has no OpenTelemetry setup:
// no-otel-preload.cjs only lets it load the TypeScript sources.
async function child(traced: boolean): Promise<any> {
  const scratch = mkdtempSync(join(root, ".traceai-groq-beside-"));
  try {
    const modules = join(scratch, "node_modules");
    mkdirSync(join(modules, "@traceai/groq"), { recursive: true });
    // Ordinary Node resolution, so the real instrumentation hooks see the name `groq-sdk`.
    symlinkSync(dirname(realpathSync(require.resolve("groq-sdk/package.json"))), join(modules, "groq-sdk"), "dir");
    // Only the source entry is redirected; no instrumentation or resolver is mocked.
    writeFileSync(join(modules, "@traceai/groq/package.json"), JSON.stringify({
      name: "@traceai/groq", main: join(root, "src/index.ts"),
    }));
    const app = join(scratch, "app.cjs");
    writeFileSync(app, readFileSync(join(__dirname, "beside-groq-call-no-otel-app.cjs")));
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      // No inherited keys, preload, privacy override, collector, or TS loader.
      if (/^(?:FI_|OTEL_|TS_NODE_|GROQ_)/.test(key) || /KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH/i.test(key) || key === "NODE_OPTIONS") delete env[key];
    }
    Object.assign(env, { NODE_OPTIONS: "", FI_API_KEY: "mock-fi-api-key", FI_SECRET_KEY: "mock-fi-secret-key",
      FI_BASE_URL: url, FI_INSTRUMENTATIONS: "groq,wire", FI_PROJECT_NAME: "groq-no-otel-test",
      MOCK_URL: url, SDK_PROMPT, PLAIN_PROMPT });
    const args = ["--no-deprecation", "-r", join(__dirname, "no-otel-preload.cjs"), ...(traced ? ["-r", auto] : []), app, traced ? "traced" : "baseline"];
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

test("in an application with no OpenTelemetry of its own, a plain fetch AI request sent in the same tick as a Groq call is not lost, and the call keeps its model span", async () => {
  // First the application without the agent: both of its requests reach the provider
  // and both answers come back. A failure up to here is in the fixture, not a lost span.
  const baseline = await child(false);
  expect(baseline.application).toEqual({ text: "hello world", plain: "hello world" });
  expect([...received].sort()).toEqual(BOTH);
  expect(collected).toHaveLength(0);
  received = [];
  // Then with the agent. The application behaves the same, and the provider gets both
  // requests, each once.
  const actual = await child(true);
  expect(actual.application).toEqual(baseline.application);
  expect([...received].sort()).toEqual(BOTH);
  // The case under test: the agent is on with the Groq integration loaded, and the
  // application still has no context manager.
  expect({ active: actual.active, groq: actual.groq, contextManager: actual.contextManager })
    .toEqual({ active: true, groq: "instrumented", contextManager: "NoopContextManager" });

  // What Future AGI received, shown when one of the two checks below fails.
  const spans = exportedSpans();
  const kind = (span: any) => attrs(span)["gen_ai.span.kind"] ?? attrs(span)["fi.span.kind"];
  const got = spans.map(span => `${span.name} (${attrs(span)["traceai.capture.layer"]}, ${kind(span)})`);
  const carries = (span: any, text: string) => JSON.stringify(span).includes(text);
  // The Groq call arrives as its model span: an LLM span from the integration, not from
  // wire capture, with the question the application gave the SDK.
  const model = spans.filter(span => attrs(span)["traceai.capture.layer"] !== "wire" && kind(span) === "LLM" && carries(span, SDK_PROMPT));
  expect({ got, modelSpan: model.length > 0 }).toEqual({ got, modelSpan: true });
  // The plain fetch arrives as a wire span with its own question. It is found by that
  // text, so the SDK's own request, if it also arrives as a wire span, cannot stand in for it.
  const plain = spans.filter(span => attrs(span)["traceai.capture.layer"] === "wire" && carries(span, PLAIN_PROMPT));
  expect({ got, plainFetchWireSpan: plain.length > 0 }).toEqual({ got, plainFetchWireSpan: true });
}, 60000);
