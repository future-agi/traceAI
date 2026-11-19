const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { setTimeout: delay } = require("node:timers/promises");
const core = require("@traceai/agent-core");
const { capture } = require("../dist/capture.js");

const attrs = (span) => Object.fromEntries(span.attributes.map((a) => [a.key, Object.values(a.value)[0]]));

async function fixture(t, terminal = true) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "grok-completion-"));
  const spans = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      for (const resource of JSON.parse(body).resourceSpans)
        for (const scope of resource.scopeSpans) spans.push(...scope.spans);
      res.setHeader("Content-Type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await fs.rm(home, { recursive: true, force: true });
  });
  const options = { agent: "grok", home, env: {} };
  await core.setup({
    ...options,
    credentials: { apiKey: "synthetic-api", secretKey: "synthetic-secret" },
    projectName: "grok-completion",
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    configureOnly: true,
  });
  const config = await core.resolveConfig(options);
  const file = path.join(home, ".grok/sessions/session/updates.jsonl");
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const started = Date.now();
  const event = (name) => ({
    event: name, sessionId: "session", promptId: "prompt", time: Date.now(),
    outcome: "success", transcriptPath: file,
  });
  const row = (update) => JSON.stringify({
    params: { sessionId: "session", update, _meta: {
      promptId: "prompt", eventId: "generation", streamStartMs: started,
      agentTimestampMs: Date.now(),
    } },
  }) + "\n";
  await fs.writeFile(file, row({ sessionUpdate: "agent_message_chunk", content: { text: "Synthetic answer" } }), { mode: 0o600 });
  await capture(event("UserPromptSubmit"), config);
  if (terminal) await capture(event("Stop"), config);
  assert.equal(spans.length, 0, "Stop returns without prematurely claiming an unfinished turn");
  const complete = () => fs.appendFile(file, row({
    sessionUpdate: "turn_completed", prompt_id: "prompt", stop_reason: "end_turn",
    usage: { inputTokens: 80, outputTokens: 20 },
  }));
  const hook = (budget = 12000) => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(__dirname, "../dist/hook.js")], {
      cwd: home, env: { HOME: home, PATH: process.env.PATH },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const timeout = setTimeout(() => child.kill("SIGKILL"), budget);
    let stdout = "", stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", reject);
    child.once("close", (code, signal) => {
      clearTimeout(timeout);
      resolve({ code, signal, stdout, stderr });
    });
    child.stdin.end(JSON.stringify({
      hookEventName: "SessionEnd", sessionId: "session", promptId: "prompt",
      timestamp: new Date().toISOString(), transcriptPath: file,
    }));
  });
  return { config, file, spans, event, complete, hook };
}

test("native SessionEnd waits for a six-second-late completion within its hook budget", { timeout: 15000 }, async (t) => {
  const f = await fixture(t);
  const late = delay(6000).then(f.complete);
  const [result] = await Promise.all([f.hook(), late]);
  assert.deepEqual(result, { code: 0, signal: null, stdout: "", stderr: "" });
  assert.equal(f.spans.length, 2, "one root and one generation, despite repeated transcript polls");
  const root = f.spans.find((s) => !s.parentSpanId);
  assert.equal(attrs(root)["fi.agent.outcome"], "success");
  const owners = f.spans.filter((s) => attrs(s)["gen_ai.usage.total_tokens"] !== undefined);
  assert.equal(owners.length, 1);
  assert.equal(attrs(owners[0])["fi.span.kind"], "LLM");
  assert.equal(Number(attrs(owners[0])["gen_ai.usage.total_tokens"]), 100);
  await capture(f.event("SessionEnd"), f.config);
  assert.equal(f.spans.length, 2, "repeated terminal hooks cannot replay the claimed turn");
});

test("missing completion exports incomplete before the native deadline without invented usage", { timeout: 15000 }, async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await f.hook(), { code: 0, signal: null, stdout: "", stderr: "" });
  assert.equal(f.spans.length, 2, "bounded wait must export rather than hit the process-exit timer");
  const root = f.spans.find((s) => !s.parentSpanId);
  assert.equal(attrs(root)["fi.agent.outcome"], "incomplete");
  assert.equal(attrs(root)["fi.agent.gap"], "MISSING_TURN_COMPLETION");
  assert.ok(f.spans.every((s) => attrs(s)["gen_ai.usage.total_tokens"] === undefined));
  await f.complete();
  await capture(f.event("SessionEnd"), f.config);
  assert.equal(f.spans.length, 2, "an expired claimed turn is never ambiguously re-exported");
});

test("bare shutdown without a prompt terminal stays within Grok's 1.5-second shutdown cap", { timeout: 5000 }, async (t) => {
  const f = await fixture(t, false);
  assert.deepEqual(await f.hook(1500), { code: 0, signal: null, stdout: "", stderr: "" });
  assert.equal(f.spans.length, 2);
  assert.equal(attrs(f.spans.find((s) => !s.parentSpanId))["fi.agent.outcome"], "incomplete");
  assert.ok(f.spans.every((s) => attrs(s)["gen_ai.usage.total_tokens"] === undefined));
});

test("overlapping terminal hooks share one claim after a split late completion record", { timeout: 10000 }, async (t) => {
  const f = await fixture(t);
  const record = JSON.stringify({ params: { sessionId: "session", update: {
    sessionUpdate: "turn_completed", prompt_id: "prompt", stop_reason: "end_turn",
    usage: { inputTokens: 80, outputTokens: 20 },
  } } });
  await fs.appendFile(f.file, record.slice(0, 40));
  const late = delay(300).then(() => fs.appendFile(f.file, record.slice(40) + "\n"));
  await Promise.all([
    capture(f.event("SessionEnd"), f.config),
    capture(f.event("SessionEnd"), f.config),
    capture(f.event("Stop"), f.config),
    late,
  ]);
  assert.equal(f.spans.filter((s) => !s.parentSpanId).length, 1);
  assert.equal(f.spans.length, 2);
  assert.equal(attrs(f.spans.find((s) => !s.parentSpanId))["fi.agent.outcome"], "success");
});
