const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, core, fs, path, spans, attributes, payload } = require("./helpers.cjs");
const { register, unregister, installRuntime, editHostFile } = require("../dist/registration.js");
const { createCliStreamObserver } = require("../dist/cli/stream.js");
const { normalizeEvent } = require("../dist/events.js");
const { CursorObserver } = require("../dist/observer.js");

test("uninstall deletes an unchanged hooks file we created, including after repeat setup", async (t) => {
  const f = await fixture(t);
  const runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  await register(f.context, runtime);
  await unregister(f.context);
  await assert.rejects(fs.stat(path.join(f.home, ".cursor/hooks.json")), { code: "ENOENT" });
  await assert.rejects(fs.stat(runtime), { code: "ENOENT" });
});

test("uninstall keeps subsequent host comments and unrelated settings", async (t) => {
  const f = await fixture(t);
  const runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const file = path.join(f.home, ".cursor/hooks.json");
  const changed = (await fs.readFile(file, "utf8")).replace("{", "{\n// User comment\n");
  await fs.writeFile(file, changed);
  await register(f.context, runtime);
  await unregister(f.context);
  assert.match(await fs.readFile(file, "utf8"), /User comment/);
});

test("legacy host claim files are inert and concurrent host merges preserve both writers", async (t) => {
  const f = await fixture(t);
  const file = path.join(f.home, ".cursor/hooks.json");
  await fs.writeFile(file + ".futureagi-lock", "legacy crashed writer", { mode: 0o600 });
  await Promise.all(["first", "second"].map((key) => editHostFile(file, (before) =>
    JSON.stringify({ ...(before ? JSON.parse(before) : { version: 1, hooks: {} }), [key]: true }))));
  const result = JSON.parse(await fs.readFile(file));
  assert.equal(result.first, true); assert.equal(result.second, true);
  assert.equal(await fs.readFile(file + ".futureagi-lock", "utf8"), "legacy crashed writer");
});

test("unsafe project settings fall back to the approved user layer", async (t) => {
  const f = await fixture(t);
  const projectDir = path.join(f.home, "repo");
  await fs.mkdir(path.join(projectDir, ".cursor"), { recursive: true });
  await fs.writeFile(path.join(projectDir, ".cursor/futureagi.json"), "{malformed");
  const config = await core.resolveConfig({ ...f.options, projectDir });
  assert.ok(config.issues.includes("PROJECT_CONFIG_IGNORED"));
  assert.equal(config.enabled, true);
  assert.equal(config.captureContent, true);
  assert.equal(config.projectName, f.config.projectName);
});

test("CLI fields reach the unified redactor before truncation and tool ids use canonical semantics", async (t) => {
  const f = await fixture(t);
  const tracer = core.createTurnTracer({ config: f.config });
  const observer = createCliStreamObserver({ tracer, runId: "redaction", ownership: "exclusive-stream" });
  const push = (event) => observer.push(Buffer.from(JSON.stringify({ session_id: "session", ...event }) + "\n"));
  push({ type: "tool_call", subtype: "completed", call_id: "source-id", tool_call: {
    shellToolCall: { args: { authorization: "Basic " + "S".repeat(21000), api_key: "PRIVATE_JSON" }, result: { success: { exitCode: 0 } } },
  } });
  push({ type: "result", subtype: "success", result: "x".repeat(19990) + f.config.credentials.secretKey });
  observer.finish(0);
  await tracer.shutdown();
  const all = spans(f.requests);
  const tool = all.find((s) => s.parentSpanId);
  assert.equal(attributes(tool)["tool_call.id"], "source-id");
  assert.equal(attributes(tool)["tool.name"], "shellToolCall");
  const wire = JSON.stringify(f.requests.map((r) => r.body));
  assert.ok(!wire.includes("PRIVATE_JSON"));
  assert.ok(!wire.includes("SSSSSS"));
  assert.ok(!wire.includes("synthetic-"), "credential prefixes must not survive truncation");
});

test("CLI failure envelopes respect FI_HIDE_ERROR_DETAILS under the unified core", async (t) => {
  const f = await fixture(t);
  const config = await core.resolveConfig({ ...f.options, env: { FI_HIDE_ERROR_DETAILS: "true" } });
  const tracer = core.createTurnTracer({ config });
  const observer = createCliStreamObserver({ tracer, runId: "failure", ownership: "exclusive-stream" });
  for (const event of [
    { type: "tool_call", subtype: "completed", call_id: "source-id", tool_call: {
      shellToolCall: { result: { content: [{ type: "text", text: JSON.stringify({ success: false, detail: "PRIVATE_FAILURE" }) }] } },
    } },
    { type: "result", subtype: "success", result: "Recovered" },
  ]) observer.push(Buffer.from(JSON.stringify({ session_id: "session", ...event }) + "\n"));
  observer.finish(0); await tracer.shutdown();
  const tool = spans(f.requests).find((s) => s.parentSpanId);
  assert.equal(attributes(tool)["fi.agent.outcome"], "error");
  assert.ok(!JSON.stringify(f.requests).includes("PRIVATE_FAILURE"));
});

test("Cloud observer does not borrow transcripts, model calls or totals from IDE semantics", async (t) => {
  const f = await fixture(t);
  const config = f.config; // Exercise the Cloud translation without forging a different approved route.
  const observer = new CursorObserver(config, undefined, undefined, "cloud");
  for (const [name, data] of [
    ["postToolUse", { tool_use_id: "cloud-id", tool_name: "MCP:search", tool_output: { redacted: true } }],
    ["stop", { status: "completed", input_tokens: 100, output_tokens: 20, transcript_path: "/must-not-read" }],
  ]) {
    const event = normalizeEvent(payload(name, data), name, config, "cloud");
    assert.equal(event.transcript, undefined);
    assert.equal(event.usage, undefined);
    await observer.accept(event);
  }
  await observer.close();
  const all = spans(f.requests);
  assert.equal(all.length, 2);
  const root = all.find((s) => !s.parentSpanId);
  assert.equal(attributes(root)["fi.agent.unit"], "run");
  assert.equal(attributes(root)["fi.agent.outcome"], "incomplete");
  assert.match(attributes(root)["fi.agent.gaps"], /PROMPT_NOT_OBSERVED/);
  assert.ok(all.every((s) => attributes(s)["fi.span.kind"] !== "LLM"));
  assert.ok(all.every((s) => attributes(s)["gen_ai.usage.input_tokens"] === undefined));
});

test("metadata scans share the snapshot lease with atomic writers", async (t) => {
  const f = await fixture(t);
  const { appendEvent, finisherStatus, pendingTurnIds } = require("../dist/state.js");
  await appendEvent(f.config, normalizeEvent(payload("beforeSubmitPrompt", { prompt: "hello" }), "beforeSubmitPrompt", f.config));
  await Promise.all([
    (async () => {
      for (let i = 0; i < 30; i++)
        await appendEvent(f.config, normalizeEvent(payload("postToolUse", {
          tool_use_id: `tool-${i}`, tool_name: "Read", tool_output: "bounded",
        }), "postToolUse", f.config));
    })(),
    (async () => {
      for (let i = 0; i < 30; i++) {
        await finisherStatus(f.config);
        await pendingTurnIds(f.config);
      }
    })(),
  ]);
});
