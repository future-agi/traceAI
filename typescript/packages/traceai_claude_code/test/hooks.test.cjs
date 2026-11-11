const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const core = require("@traceai/agent-core");
const adapter = require("../dist/index.js");
const { handleHook } = require("../dist/hook.js");
const session = "8a472dc2-ce2f-48ef-ade3-07ec9791f6d3",
  prompt = "0bb7337e-58a1-48f0-806d-492fca3487db";
async function fixture(t, overrides = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-claude-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const received = [];
  const server = http.createServer(async (req, res) => {
    received.push(JSON.parse(Buffer.concat(await Array.fromAsync(req))));
    res.writeHead(200, { "content-type": "application/json" }).end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const env = {
    HOME: home,
    CLAUDE_CONFIG_DIR: home + "/.claude",
    XDG_DATA_HOME: home + "/.local/share",
    // Hermetic: CI runners export their own XDG_CONFIG_HOME.
    XDG_CONFIG_HOME: home + "/.config",
    FI_API_KEY: "dummy-fi-api",
    FI_SECRET_KEY: "dummy-fi-secret",
    FI_PROJECT_NAME: "claude-test",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces`,
    FI_AGENT_ENABLED: "true",
    ...overrides,
  };
  const original = { ...process.env };
  Object.assign(process.env, env);
  t.after(() => {
    for (const k of Object.keys(process.env))
      if (!(k in original)) delete process.env[k];
    Object.assign(process.env, original);
  });
  const config = await core.resolveConfig({ agent: "claude", home, env });
  const directory = home + "/.claude/projects/sample";
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const transcript = directory + "/" + session + ".jsonl";
  await fs.writeFile(
    transcript,
    await fs.readFile(
      path.join(__dirname, "fixtures/hooks-transcript-2.1.318.jsonl"),
    ),
    { mode: 0o600 },
  );
  const st = await fs.stat(transcript);
  assert.equal(st.uid, process.getuid(), "fixture owner");
  assert.equal(st.nlink, 1, "fixture hard links");
  const state = {
    schemaVersion: 1,
    offset: 0,
    promptId: prompt,
    start: 1793958308016,
    tools: { toolu_probe: { start: 1793958308102, end: 1793958308204 } },
    subagents: [],
    compactions: [],
    completed: [],
  };
  return {
    home,
    env,
    config,
    transcript,
    state,
    received,
    context: { config, scope: "user" },
  };
}
const spans = (b) =>
  b.flatMap((x) =>
    (x.resourceSpans || []).flatMap((r) =>
      r.scopeSpans.flatMap((s) => s.spans),
    ),
  );
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [
      a.key,
      a.value.stringValue ?? a.value.intValue ?? a.value.boolValue,
    ]),
  );
test("actual transcript emits one turn, two generations, tool content and exact usage ownership", async (t) => {
  const f = await fixture(t);
  const result = await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  assert.equal(result.generations, 2);
  assert.equal(result.tools, 1);
  assert.equal(result.outcome, "success");
  const s = spans(f.received),
    llms = s.filter((x) => attrs(x)["fi.span.kind"] === "LLM");
  assert.equal(s.length, 4);
  assert.equal(llms.length, 2);
  for (const llm of llms) {
    const a = attrs(llm);
    assert.equal(a["gen_ai.usage.input_tokens"], 27);
    assert.equal(a["gen_ai.usage.output_tokens"], 11);
    assert.equal(a["gen_ai.usage.cache_read_tokens"], 7);
    assert.equal(a["gen_ai.usage.cache_write_tokens"], 3);
    assert.equal(a["fi.agent.fidelity"], "reconstructed-message");
    assert.equal(a["fi.agent.timing"], "reconstructed");
    assert.equal(a["gen_ai.usage.cost"], undefined);
  }
  assert(
    llms.some((x) => attrs(x)["output.value"].includes("SAMPLE_MODEL_OUTPUT")),
  );
  const tool = s.find((x) => attrs(x)["fi.span.kind"] === "TOOL");
  assert.match(attrs(tool)["input.value"], /file_path/);
  assert.match(attrs(tool)["output.value"], /SAMPLE_TOOL_OUTPUT/);
  assert(llms.some((x) => x.spanId === tool.parentSpanId));
  const root = s.find((x) => !x.parentSpanId);
  assert.equal(attrs(root)["gen_ai.usage.input_tokens"], undefined);
});
test("streaming fragments with the same request and message id contribute usage once", async (t) => {
  const f = await fixture(t);
  const rows = (await fs.readFile(f.transcript, "utf8"))
    .trim()
    .split("\n")
    .map(JSON.parse);
  const i = rows.findIndex((r) => r.type === "assistant");
  const copy = structuredClone(rows[i]);
  copy.uuid = "different";
  copy.message.content = [{ type: "text", text: "fragment" }];
  rows.splice(i + 1, 0, copy);
  await fs.writeFile(f.transcript, rows.map(JSON.stringify).join("\n") + "\n");
  const result = await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  assert.equal(result.generations, 2);
  assert.equal(
    spans(f.received).filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    2,
  );
});
test("privacy mode and stricter FI switches strip all content without dropping structure", async (t) => {
  const f = await fixture(t, { FI_AGENT_CAPTURE_CONTENT: "false" });
  await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  const body = JSON.stringify(f.received);
  assert(!body.includes("SAMPLE_"));
  assert(!body.includes("input.value"));
  assert(!body.includes("output.value"));
  assert(!body.includes("filePath"));
  assert.equal(spans(f.received).length, 4);
});
test("transcript reader rejects symlinks, paths outside host and truncation", async (t) => {
  const f = await fixture(t);
  await assert.rejects(
    adapter.readTranscript("/etc/passwd", f.config, 0),
    /TRANSCRIPT_OUTSIDE_HOST/,
  );
  await assert.rejects(
    adapter.readTranscript(f.transcript, f.config, 99999999),
    /TRANSCRIPT_TRUNCATED/,
  );
  const link = path.join(path.dirname(f.transcript), "link.jsonl");
  await fs.symlink(f.transcript, link);
  await assert.rejects(adapter.readTranscript(link, f.config, 0));
  await fs.appendFile(f.transcript, '{"type":');
  const read = await adapter.readTranscript(f.transcript, f.config, 0);
  assert.equal(read.partial, true);
  assert(read.rows.length >= 4);
});
test("installer merges user hooks, pins durable runtime, repeats safely and uninstalls only owned entries", async (t) => {
  const f = await fixture(t);
  const settings = path.join(
    path.dirname(f.config.paths.user),
    "settings.json",
  );
  await core.updateJson(settings, () => ({
    model: "keep-model",
    hooks: {
      Stop: [{ hooks: [{ type: "command", command: "echo unrelated" }] }],
    },
    env: { UNRELATED: "keep" },
  }));
  await fs.chmod(settings, 0o644);
  await adapter.installHooks(f.context);
  await fs.chmod(settings, 0o644);
  await adapter.installHooks(f.context);
  const data = JSON.parse(await fs.readFile(settings, "utf8"));
  assert.equal(data.model, "keep-model");
  assert.equal(data.env.UNRELATED, "keep");
  assert.equal(data.hooks.Stop.length, 2);
  assert.equal(data.hooks.UserPromptSubmit.length, 1);
  assert(!JSON.stringify(data).includes("dummy-fi"));
  const owned = data.hooks.Stop[1].hooks[0].command;
  assert(owned.includes("/.local/share/futureagi/agents/runtime/claude-"));
  assert(!owned.includes("/node_modules/"));
  assert.equal((await fs.stat(settings)).mode & 0o777, 0o600);
  await fs.chmod(settings, 0o644);
  await adapter.uninstallHooks(f.context);
  const removed = JSON.parse(await fs.readFile(settings, "utf8"));
  assert.equal(removed.hooks.Stop.length, 1);
  assert.equal(removed.hooks.Stop[0].hooks[0].command, "echo unrelated");
  assert.equal(removed.model, "keep-model");
});
test("installer refuses native duplication, JSONC and symlinks without changing user settings", async (t) => {
  const f = await fixture(t);
  const file = path.join(path.dirname(f.config.paths.user), "settings.json");
  await core.updateJson(file, () => ({
    env: { CLAUDE_CODE_ENHANCED_TELEMETRY_BETA: "1" },
  }));
  const before = await fs.readFile(file, "utf8");
  await assert.rejects(
    adapter.installHooks(f.context),
    /NATIVE_TRACING_CONFLICT/,
  );
  assert.equal(await fs.readFile(file, "utf8"), before);
  await fs.writeFile(file, '{ // preserve comment\n "model": "keep"\n}', {
    mode: 0o600,
  });
  await assert.rejects(adapter.installHooks(f.context), /INVALID_CONFIG_JSON/);
  await fs.unlink(file);
  await fs.symlink(f.transcript, file);
  await assert.rejects(adapter.installHooks(f.context));
});
test("hook state is metadata only and Stop/SessionEnd/repeated Stop finalize once", async (t) => {
  const f = await fixture(t);
  const base = {
    session_id: session,
    prompt_id: prompt,
    transcript_path: f.transcript,
    cwd: f.home,
  };
  await handleHook({
    ...base,
    hook_event_name: "UserPromptSubmit",
    prompt: "PRIVATE_PROMPT_SENTINEL",
  });
  await handleHook({
    ...base,
    hook_event_name: "PreToolUse",
    tool_use_id: "toolu_probe",
    tool_input: { secret: "PRIVATE_TOOL_SENTINEL" },
  });
  await handleHook({
    ...base,
    hook_event_name: "PostToolUse",
    tool_use_id: "toolu_probe",
    tool_response: { secret: "PRIVATE_TOOL_SENTINEL" },
  });
  const file = path.join(
    f.config.paths.configDir,
    "claude-sessions",
    core.contentHash(session) + ".json",
  );
  assert(!(await fs.readFile(file, "utf8")).includes("PRIVATE_"));
  await handleHook({ ...base, hook_event_name: "Stop" });
  await handleHook({ ...base, hook_event_name: "Stop" });
  await handleHook({ ...base, hook_event_name: "SessionEnd" });
  assert.equal(spans(f.received).length, 4);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
});
test("persistent disable fences export without credentials or native telemetry", async (t) => {
  const f = await fixture(t);
  await core.disable({ agent: "claude", home: f.home, env: f.env });
  const base = {
    session_id: session,
    prompt_id: prompt,
    transcript_path: f.transcript,
    cwd: f.home,
  };
  await handleHook({ ...base, hook_event_name: "UserPromptSubmit" });
  await handleHook({ ...base, hook_event_name: "Stop" });
  assert.equal(f.received.length, 0);
  assert.equal(adapter.claudeAdapter.requiresWorker, false);
});
test("concurrent terminal hooks finalize once and a killed-hook stale lock recovers", async (t) => {
  const f = await fixture(t);
  const base = {
    session_id: session,
    prompt_id: prompt,
    transcript_path: f.transcript,
    cwd: f.home,
  };
  const directory = path.join(f.config.paths.configDir, "claude-sessions");
  await core.ensurePrivateDirectory(directory);
  const lock = path.join(
    directory,
    core.contentHash(session) + ".json.event.write-lock",
  );
  await fs.mkdir(lock, { mode: 0o700 });
  const old = new Date(Date.now() - 10000);
  await fs.utimes(lock, old, old);
  await handleHook({ ...base, hook_event_name: "UserPromptSubmit" });
  await Promise.all([
    handleHook({ ...base, hook_event_name: "Stop" }),
    handleHook({ ...base, hook_event_name: "SessionEnd" }),
  ]);
  assert.equal(spans(f.received).length, 4);
  await assert.rejects(fs.stat(lock), { code: "ENOENT" });
});
test("real async completion notification is not a new user trace and child usage is counted once", async (t) => {
  const f = await fixture(t);
  const read = async (name) =>
    (await fs.readFile(path.join(__dirname, "fixtures", name), "utf8"))
      .trim()
      .split("\n")
      .map(JSON.parse);
  const main = await read("async-main-2.1.318.jsonl"),
    child = await read("async-child-2.1.318.jsonl");
  for (const row of [...main, ...child]) row.sessionId = session;
  await fs.writeFile(f.transcript, main.map(JSON.stringify).join("\n") + "\n");
  const childPath = path.join(path.dirname(f.transcript), "agent-child.jsonl");
  await fs.writeFile(childPath, child.map(JSON.stringify).join("\n") + "\n", {
    mode: 0o600,
  });
  for (const event of require("./fixtures/async-events-2.1.318.json"))
    await handleHook({
      ...event,
      session_id: session,
      transcript_path: f.transcript,
      cwd: f.home,
      ...(event.agent_transcript_path
        ? { agent_transcript_path: childPath }
        : {}),
    });
  const all = spans(f.received);
  assert.equal(all.filter((s) => !s.parentSpanId).length, 1);
  assert.equal(new Set(all.map((s) => s.traceId)).size, 1);
  assert.equal(all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 7);
  const agent = all.find((s) => s.name === "Agent");
  assert(agent);
  assert(
    all.some(
      (s) =>
        s.parentSpanId === agent.spanId && attrs(s)["fi.span.kind"] === "LLM",
    ),
  );
});
test("terminal text gate waits for the last assistant row even when the earlier file ends cleanly", async (t) => {
  const f = await fixture(t);
  const rows = (await fs.readFile(f.transcript, "utf8")).trim().split("\n");
  const last = rows.pop();
  await fs.writeFile(f.transcript, rows.join("\n") + "\n");
  const append = setTimeout(() => fs.appendFile(f.transcript, last + "\n"), 40);
  t.after(() => clearTimeout(append));
  const result = await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
    { terminalText: "SAMPLE_MODEL_OUTPUT: read complete." },
  );
  assert.equal(result.generations, 2);
});
test("packaged hook fails open and silently on malformed or oversized input", async (t) => {
  const f = await fixture(t);
  for (const input of ["not json", "x".repeat(300000)]) {
    const result = await new Promise((r) => {
      const child = spawn(
        process.execPath,
        [path.join(__dirname, "../dist/hook.cjs")],
        { env: { ...process.env, ...f.env }, stdio: ["pipe", "pipe", "pipe"] },
      );
      let output = "";
      child.stdout.on("data", (d) => (output += d));
      child.stderr.on("data", (d) => (output += d));
      child.on("exit", (code) => r({ code, output }));
      child.stdin.on("error", () => {});
      child.stdin.end(input);
    });
    assert.deepEqual(result, { code: 0, output: "" });
  }
});

async function rowsFor(f, id, label) {
  const raw = await fs.readFile(
    path.join(__dirname, "fixtures/hooks-transcript-2.1.318.jsonl"),
    "utf8",
  );
  return (
    raw
      .replaceAll(prompt, id)
      .replaceAll(
        "/work/project/sample.txt",
        "/work/" + label + "_TOOL_ARGUMENT.txt",
      )
      .replaceAll("SAMPLE_", label + "_")
      .trim()
      .split("\n")
      .map(JSON.parse)
      .map((r) => {
        if (r.requestRef) r.requestRef += id;
        if (r.message?.id) r.message.id += id;
        return JSON.stringify(r);
      })
      .join("\n") + "\n"
  );
}
async function hooks(f, id, names) {
  for (const hook_event_name of names)
    await handleHook({
      session_id: session,
      prompt_id: id,
      transcript_path: f.transcript,
      cwd: f.home,
      hook_event_name,
    });
}
test("world-readable host settings are accepted but group/other writable settings are rejected", async (t) => {
  const f = await fixture(t);
  const file = path.join(path.dirname(f.config.paths.user), "settings.json");
  await fs.writeFile(file, '{"model":"keep"}', { mode: 0o600 });
  for (const mode of [0o664, 0o666]) {
    await fs.chmod(file, mode);
    await assert.rejects(adapter.installHooks(f.context), /UNSAFE_FILE/);
    assert.equal(await fs.readFile(file, "utf8"), '{"model":"keep"}');
  }
});
for (const mode of ["disabled", "content-off", "mid-turn-opt-in"])
  test(`${mode} history stays private after re-enabling`, async (t) => {
    const f = await fixture(t);
    await fs.writeFile(f.transcript, await rowsFor(f, prompt, "PRIVATE"));
    if (mode === "disabled") process.env.FI_AGENT_ENABLED = "false";
    else process.env.FI_AGENT_CAPTURE_CONTENT = "false";
    await hooks(f, prompt, ["UserPromptSubmit"]);
    if (mode === "mid-turn-opt-in")
      process.env.FI_AGENT_CAPTURE_CONTENT = "true";
    await hooks(f, prompt, ["Stop", "SessionEnd"]);
    assert(!JSON.stringify(f.received).includes("PRIVATE_"));
    assert.equal(spans(f.received).length, mode === "disabled" ? 0 : 4);
    process.env.FI_AGENT_ENABLED = "true";
    process.env.FI_AGENT_CAPTURE_CONTENT = "true";
    const next = "second-prompt";
    await fs.appendFile(f.transcript, await rowsFor(f, next, "ALLOWED"));
    await hooks(f, next, ["UserPromptSubmit", "Stop", "SessionEnd"]);
    const body = JSON.stringify(f.received);
    assert(
      !body.includes("PRIVATE_"),
      "private history escaped through generation inputs",
    );
    assert(body.includes("ALLOWED_MODEL_OUTPUT"));
  });
test("large existing history attaches at current prompt without unbounded reads", async (t) => {
  const f = await fixture(t);
  const turn = await fs.readFile(f.transcript, "utf8");
  await fs.writeFile(
    f.transcript,
    JSON.stringify({ history: "x".repeat(9 * 1024 * 1024) }) + "\n" + turn,
  );
  await hooks(f, prompt, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  assert.equal(spans(f.received).length, 4);
});
test("an oversized turn records a gap and the following small turn still exports", async (t) => {
  const f = await fixture(t);
  await hooks(f, prompt, ["UserPromptSubmit"]);
  await fs.appendFile(
    f.transcript,
    JSON.stringify({ history: "x".repeat(9 * 1024 * 1024) }) + "\n",
  );
  await hooks(f, prompt, ["Stop"]);
  assert.equal(spans(f.received).length, 0);
  const diagnostic = JSON.parse(
    await fs.readFile(path.join(f.config.paths.stateDir, "claude-hook.json")),
  );
  assert.equal(diagnostic.code, "TRANSCRIPT_LIMIT");
  const next = "small-prompt";
  await fs.appendFile(f.transcript, await rowsFor(f, next, "RECOVERED"));
  await hooks(f, next, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  assert.equal(spans(f.received).length, 4);
  assert(JSON.stringify(f.received).includes("RECOVERED_MODEL_OUTPUT"));
});
test("blocked Stop continuation adds usage once and publishes only the actual final root", async (t) => {
  const f = await fixture(t);
  await hooks(f, prompt, ["UserPromptSubmit", "Stop"]);
  assert.equal(spans(f.received).filter((s) => !s.parentSpanId).length, 0);
  const continuation = (await rowsFor(f, prompt, "FINAL"))
    .trim()
    .split("\n")
    .map(JSON.parse)
    .filter(
      (r) =>
        r.type !== "user" ||
        r.message?.content?.some?.((b) => b.type === "tool_result"),
    );
  for (const r of continuation) {
    if (Array.isArray(r.message?.content))
      for (const b of r.message.content) {
        if (b.id) b.id += "-continued";
        if (b.tool_use_id) b.tool_use_id += "-continued";
      }
  }
  await fs.appendFile(
    f.transcript,
    continuation.map(JSON.stringify).join("\n") + "\n",
  );
  await hooks(f, prompt, ["Stop", "Stop", "SessionEnd", "SessionEnd"]);
  const all = spans(f.received),
    roots = all.filter((s) => !s.parentSpanId),
    llms = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(roots.length, 1);
  assert.match(attrs(roots[0])["output.value"], /FINAL_MODEL_OUTPUT/);
  // The root shows the final reply as plain text, not serialized content blocks.
  assert.doesNotMatch(attrs(roots[0])["output.value"], /^\s*\[|"type"\s*:\s*"text"/);
  assert.equal(llms.length, 4);
  assert.equal(new Set(all.map((s) => s.spanId)).size, all.length);
  assert.equal(new Set(all.map((s) => s.traceId)).size, 1);
});

test("continued tool work without terminal evidence is incomplete after an attempted Stop", async (t) => {
  const f = await fixture(t);
  await hooks(f, prompt, [
    "UserPromptSubmit",
    "Stop",
    "PreToolUse",
    "SessionEnd",
  ]);
  const root = spans(f.received).find((s) => !s.parentSpanId);
  assert.equal(attrs(root)["fi.agent.outcome"], "incomplete");
});

test("queued human prompts finalize independently without repeated roots or usage", async (t) => {
  const f = await fixture(t);
  await hooks(f, prompt, ["UserPromptSubmit"]);
  const next = "queued-human";
  await fs.appendFile(f.transcript, await rowsFor(f, next, "QUEUED"));
  await hooks(f, next, ["UserPromptSubmit", "Stop", "Stop", "SessionEnd"]);
  const all = spans(f.received);
  assert.equal(all.filter((s) => !s.parentSpanId).length, 2);
  assert.equal(all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 4);
  assert.equal(new Set(all.map((s) => s.spanId)).size, all.length);
});

test("qualified major-line updates export; unknown transcript shapes give an incomplete root", async (t) => {
  const f = await fixture(t);
  assert.equal(adapter.supportedHost("2.2.999"), true);
  assert.equal(adapter.supportedHost("3.0.0"), false);
  const original = await fs.readFile(f.transcript, "utf8");
  await fs.writeFile(f.transcript, original.replaceAll("2.1.318", "2.2.999"));
  await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  assert.equal(spans(f.received).length, 4);
  f.received.length = 0;
  await fs.writeFile(f.transcript, original.replaceAll("2.1.318", "3.0.0"));
  const result = await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  assert.equal(result.outcome, "incomplete");
  assert.equal(spans(f.received).length, 1);
});
test("late terminal transcript waits beyond sub-second startup without losing the turn", async (t) => {
  const f = await fixture(t);
  const lines = (await fs.readFile(f.transcript, "utf8")).trim().split("\n");
  const last = lines.pop();
  await fs.writeFile(f.transcript, lines.join("\n") + "\n");
  const append = setTimeout(
    () => fs.appendFile(f.transcript, last + "\n"),
    450,
  );
  t.after(() => clearTimeout(append));
  const result = await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
    { terminalText: "SAMPLE_MODEL_OUTPUT: read complete." },
  );
  assert.equal(result.generations, 2);
});
test("stable interpreter symlink is pinned, missing runtime is diagnosed and uninstall prunes only owned bundle", async (t) => {
  const f = await fixture(t);
  const bin = path.join(f.home, "bin");
  await fs.mkdir(bin);
  await fs.chmod(bin, 0o775); // Host metadata/interpreter discovery is not a protected config read.
  await fs.symlink(process.execPath, path.join(bin, "node"));
  await fs.writeFile(
    path.join(bin, "claude"),
    '#!/bin/sh\nprintf "2.2.999 (Claude Code)\\n"\n',
    { mode: 0o700 },
  );
  process.env.PATH = bin + path.delimiter + process.env.PATH;
  await adapter.installHooks(f.context);
  const registration = await core.readJson(
    path.join(f.config.paths.configDir, "claude-hooks.json"),
  );
  assert.equal(registration.node, path.join(bin, "node"));
  assert.equal((await adapter.inspectHooks(f.context)).registered, true);
  await fs.unlink(path.join(bin, "node"));
  assert.equal(
    (await adapter.inspectHooks(f.context)).detail,
    "RUNTIME_MISSING_OR_CHANGED",
  );
  const status = await core.inspectStatus({
    agent: "claude",
    home: f.home,
    env: f.env,
    adapter: adapter.claudeAdapter,
  });
  assert(status.issues.includes("RUNTIME_MISSING_OR_CHANGED"));
  await adapter.uninstallHooks(f.context);
  await assert.rejects(fs.stat(path.dirname(registration.runtime)), {
    code: "ENOENT",
  });
});
async function childTurn(f, id, label, childPath) {
  const main = (await rowsFor(f, id, label)).trim().split("\n").map(JSON.parse);
  const caller = main.find(
    (r) =>
      r.type === "assistant" &&
      r.message.content.some((b) => b.type === "tool_use"),
  );
  const delegate = caller.message.content.find((b) => b.type === "tool_use");
  delegate.name = "Agent";
  delegate.input = { prompt: `${label}_CHILD_USER_PROMPT: Read sample.txt.` };
  const result = main.find((r) =>
    r.message?.content?.some?.((b) => b.type === "tool_result"),
  );
  result.toolUseResult = { agentId: "resumed-child" };
  await fs.appendFile(f.transcript, main.map(JSON.stringify).join("\n") + "\n");
  const child = (await rowsFor(f, id + "-child", label + "_CHILD"))
    .trim()
    .split("\n")
    .map(JSON.parse);
  for (const row of child) {
    row.agentId = "resumed-child";
    row.timestamp = new Date().toISOString();
  }
  return {
    child,
    base: {
      session_id: session,
      prompt_id: id,
      transcript_path: f.transcript,
      cwd: f.home,
      agent_id: "resumed-child",
      agent_transcript_path: childPath,
    },
  };
}
for (const mode of ["content-off", "disabled"])
  test(`${mode} resumed subagent never backfills private child history`, async (t) => {
    const f = await fixture(t);
    await fs.writeFile(f.transcript, "");
    const childPath = path.join(
      path.dirname(f.transcript),
      "resumed-child.jsonl",
    );
    if (mode === "disabled") process.env.FI_AGENT_ENABLED = "false";
    else process.env.FI_AGENT_CAPTURE_CONTENT = "false";
    const first = await childTurn(f, prompt, "PRIVATE", childPath);
    await hooks(f, prompt, ["UserPromptSubmit"]);
    await handleHook({ ...first.base, hook_event_name: "SubagentStart" });
    await fs.writeFile(
      childPath,
      first.child.map(JSON.stringify).join("\n") + "\n",
      { mode: 0o600 },
    );
    await handleHook({ ...first.base, hook_event_name: "SubagentStop" });
    await hooks(f, prompt, ["Stop", "SessionEnd"]);
    process.env.FI_AGENT_ENABLED = "true";
    process.env.FI_AGENT_CAPTURE_CONTENT = "true";
    const second = await childTurn(f, "allowed-prompt", "ALLOWED", childPath);
    await hooks(f, "allowed-prompt", ["UserPromptSubmit"]);
    await handleHook({ ...second.base, hook_event_name: "SubagentStart" });
    await fs.appendFile(
      childPath,
      second.child.map(JSON.stringify).join("\n") + "\n",
    );
    await handleHook({ ...second.base, hook_event_name: "SubagentStop" });
    await hooks(f, "allowed-prompt", ["Stop", "SessionEnd"]);
    assert(!JSON.stringify(f.received).includes("PRIVATE_"));
    assert(JSON.stringify(f.received).includes("ALLOWED_CHILD_MODEL_OUTPUT"));
    const allowedChild = spans(f.received).filter(
      (s) =>
        attrs(s)["fi.span.kind"] === "LLM" &&
        attrs(s)["output.value"]?.includes("ALLOWED_CHILD_MODEL_OUTPUT"),
    );
    assert.equal(allowedChild.length, 1);
    assert.match(
      attrs(allowedChild[0])["input.value"],
      /ALLOWED_CHILD_USER_PROMPT/,
    );
    const all = spans(f.received);
    assert.equal(
      all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      mode === "disabled" ? 4 : 8,
    );
  });
test("unresolved child ancestry is incomplete and is not reserved as an attempted export", async (t) => {
  const f = await fixture(t);
  const childPath = path.join(path.dirname(f.transcript), "orphan.jsonl");
  const child = (await fs.readFile(f.transcript, "utf8"))
    .trim()
    .split("\n")
    .map(JSON.parse);
  for (const row of child) row.agentId = "orphan";
  await fs.writeFile(childPath, child.map(JSON.stringify).join("\n") + "\n");
  f.state.subagents = [
    { id: "orphan", path: childPath, offset: 0, contentFloor: 0 },
  ];
  let reserved;
  const result = await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
    {
      beforeExport: async (ids) => {
        reserved = ids;
      },
    },
  );
  assert.equal(result.outcome, "incomplete");
  assert.equal(reserved.length, 3);
});
test("tool semantic names are present and nonterminal hook startup does not load the exporter", async (t) => {
  const f = await fixture(t);
  await adapter.exportTranscriptTurn(
    f.config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  const tool = spans(f.received).find(
    (s) => attrs(s)["fi.span.kind"] === "TOOL",
  );
  assert.equal(attrs(tool)["tool.name"], "Read");
  assert.equal(attrs(tool)["tool_call.id"], "toolu_probe");
  const hookPath = path.resolve(__dirname, "../dist/hook.js");
  const result = await new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        "-e",
        `require(${JSON.stringify(hookPath)});console.log(Object.keys(require.cache).some(k=>k.includes('sdk-trace-base')||k.includes('otlp-transformer')))`,
      ],
      { env: process.env },
    );
    let output = "";
    child.stdout.on("data", (d) => (output += d));
    child.on("error", reject);
    child.on("exit", (code) => resolve({ code, output }));
  });
  assert.deepEqual(result, { code: 0, output: "false\n" });
});

const statePath = (f) =>
  path.join(
    f.config.paths.configDir,
    "claude-sessions",
    core.contentHash(session) + ".json",
  );
const readRows = async (f) =>
  (await fs.readFile(f.transcript, "utf8")).trim().split("\n").map(JSON.parse);
const writeRows = (f, rows) =>
  fs.writeFile(f.transcript, rows.map(JSON.stringify).join("\n") + "\n");
const total = (all, key) =>
  all.reduce((n, s) => n + Number(attrs(s)[key] ?? 0), 0);
for (const lateSessionEnd of [false, true])
  test(`repeated terminal text requires the final response identity; late SessionEnd=${lateSessionEnd}`, async (t) => {
    const f = await fixture(t);
    const rows = await readRows(f);
    const final = rows.at(-1);
    const answer = final.message.content[0].text;
    rows[1].message.content.unshift({ type: "text", text: answer });
    await writeRows(f, rows);
    // Full-source golden pins every deterministic generation identity, not just counts.
    await adapter.exportTranscriptTurn(
      f.config,
      f.state,
      f.transcript,
      session,
      "success",
    );
    const expectedIds = spans(f.received)
      .filter((s) => attrs(s)["fi.span.kind"] === "LLM")
      .map((s) => s.spanId)
      .sort();
    f.received.length = 0;
    await writeRows(f, rows.slice(0, -1));
    await hooks(f, prompt, ["UserPromptSubmit"]);
    const stop = () =>
      handleHook({
        session_id: session,
        prompt_id: prompt,
        transcript_path: f.transcript,
        cwd: f.home,
        hook_event_name: "Stop",
        last_assistant_message: answer,
      });
    if (lateSessionEnd) {
      await stop(); // Exhaust the bounded wait; do not retire the incomplete source.
      await hooks(f, prompt, ["SessionEnd"]);
      const pending = JSON.parse(await fs.readFile(statePath(f), "utf8"));
      assert.equal(pending.ended, false);
      assert(!pending.completed.includes(prompt));
      assert(!JSON.stringify(pending).includes(answer));
      assert.equal(spans(f.received).length, 0);
      await fs.appendFile(f.transcript, JSON.stringify(final) + "\n");
    } else {
      let finalizedBeforeRow;
      const append = new Promise((resolve, reject) =>
        setTimeout(() => {
          finalizedBeforeRow = spans(f.received).some((s) => !s.parentSpanId);
          fs.appendFile(f.transcript, JSON.stringify(final) + "\n").then(
            resolve,
            reject,
          );
        }, 450),
      );
      await stop();
      await append;
      assert.equal(finalizedBeforeRow, false);
    }
    await hooks(f, prompt, ["SessionEnd", "Stop", "SessionEnd"]);
    const all = spans(f.received);
    const llms = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
    assert.deepEqual(llms.map((s) => s.spanId).sort(), expectedIds);
    assert.equal(new Set(all.map((s) => s.spanId)).size, 4);
    const roots = all.filter((s) => !s.parentSpanId);
    assert.equal(roots.length, 1);
    assert.equal(attrs(roots[0])["fi.agent.outcome"], "success");
    assert.match(
      attrs(roots[0])["output.value"],
      /SAMPLE_MODEL_OUTPUT: read complete/,
    );
    assert.equal(total(all, "gen_ai.usage.input_tokens"), 54);
    assert.equal(total(all, "gen_ai.usage.output_tokens"), 22);
    assert.equal(total(all, "gen_ai.usage.cache_read_tokens"), 14);
    assert.equal(total(all, "gen_ai.usage.cache_write_tokens"), 6);
  });

test("fresh child prompt written before SubagentStart is in every generation input", async (t) => {
  const f = await fixture(t);
  await fs.writeFile(f.transcript, "");
  const childPath = path.join(path.dirname(f.transcript), "fresh-child.jsonl");
  const child = await childTurn(f, prompt, "DELEGATED", childPath);
  for (const row of child.child) row.timestamp = "2026-01-01T00:00:00.000Z";
  await fs.writeFile(
    childPath,
    child.child.map(JSON.stringify).join("\n") + "\n",
  );
  await hooks(f, prompt, ["UserPromptSubmit"]);
  await handleHook({ ...child.base, hook_event_name: "SubagentStart" });
  await handleHook({ ...child.base, hook_event_name: "SubagentStop" });
  await hooks(f, prompt, ["Stop", "SessionEnd"]);
  const all = spans(f.received);
  const agent = all.find((s) => s.name === "Agent");
  const children = all.filter(
    (s) =>
      attrs(s)["fi.span.kind"] === "LLM" && s.parentSpanId === agent.spanId,
  );
  assert.equal(children.length, 2);
  for (const generation of children)
    assert.match(
      attrs(generation)["input.value"],
      /DELEGATED_CHILD_USER_PROMPT/,
    );
});

test("nested JSON credentials never escape through tools, model output or reconstructed history", async (t) => {
  const f = await fixture(t);
  const rows = await readRows(f);
  const secrets = Object.fromEntries(
    [
      "api_key",
      "secretKey",
      "password",
      "access_token",
      "refreshToken",
      "X-Api-Key",
      "X-Secret-Key",
      "Authorization",
      "Proxy-Authorization",
      "Cookie",
      "Set-Cookie",
    ].map((key, i) => [key, `opaque_synthetic_credential_${i}_do_not_export`]),
  );
  rows[1].message.content[0].input = { safe: "visible", nested: [secrets] };
  rows[2].message.content[0].content = JSON.stringify({ headers: secrets });
  rows[3].message.content[0].text = `example credentials: ${JSON.stringify(secrets)}`;
  await writeRows(f, rows);
  await hooks(f, prompt, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  const all = spans(f.received);
  assert.equal(all.length, 4);
  const encoded = JSON.stringify(f.received);
  for (const value of Object.values(secrets))
    assert(!encoded.includes(value), "credential escaped");
  const tool = all.find((s) => attrs(s)["fi.span.kind"] === "TOOL");
  assert.match(attrs(tool)["input.value"], /visible/);
  assert.match(attrs(tool)["output.value"], /REDACTED|<redacted>/);
  const generations = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  for (const generation of generations)
    assert.match(attrs(generation)["output.value"], /REDACTED|<redacted>/);
  assert.match(attrs(generations[1])["input.value"], /REDACTED|<redacted>/);
});

for (const viaHooks of [false, true])
  test(`multi-batch terminal drain exports 60 generations, 30 tools and root once; hooks=${viaHooks}`, async (t) => {
    const f = await fixture(t);
    const template = await readRows(f);
    const rows = [template[0]];
    for (let i = 0; i < 30; i++) {
      const triple = structuredClone(template.slice(1));
      for (const [j, row] of triple.entries()) {
        row.uuid = `long-row-${i}-${j}`;
        if (row.requestRef) row.requestRef = `request-${i}-${j}`;
        if (row.message.id) row.message.id = `message-${i}-${j}`;
        for (const b of row.message.content) {
          if (b.id) b.id = `tool-${i}`;
          if (b.tool_use_id) b.tool_use_id = `tool-${i}`;
        }
      }
      rows.push(...triple);
    }
    await writeRows(f, rows);
    if (viaHooks)
      await hooks(f, prompt, [
        "UserPromptSubmit",
        "Stop",
        "Stop",
        "SessionEnd",
        "SessionEnd",
      ]);
    else {
      const result = await adapter.exportTranscriptTurn(
        f.config,
        f.state,
        f.transcript,
        session,
        "success",
      );
      assert.equal(result.exported, 91);
      assert.equal(result.dropped, 0);
    }
    const all = spans(f.received);
    assert.equal(all.length, 91);
    assert.equal(
      all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      60,
    );
    assert.equal(
      all.filter((s) => attrs(s)["fi.span.kind"] === "TOOL").length,
      30,
    );
    assert.equal(all.filter((s) => !s.parentSpanId).length, 1);
    assert.equal(new Set(all.map((s) => s.spanId)).size, 91);
    assert.equal(total(all, "gen_ai.usage.input_tokens"), 1620);
    assert.equal(total(all, "gen_ai.usage.output_tokens"), 660);
    assert.equal(total(all, "gen_ai.usage.cache_read_tokens"), 420);
    assert.equal(total(all, "gen_ai.usage.cache_write_tokens"), 180);
    const delivery = await core.readJson(core.deliveryStatePath(f.config));
    assert.equal(delivery.exported, 91);
    assert.equal(delivery.dropped, 0);
  });

test("killed atomic writers cannot permanently block session hooks or delivery lifecycle state", async (t) => {
  const f = await fixture(t);
  const sessionFile = statePath(f);
  const deliveryFile = core.deliveryStatePath(f.config);
  // Reproduce old crash residue as well as actual crashes in the recoverable writer.
  await core.ensurePrivateDirectory(path.dirname(sessionFile));
  await core.ensurePrivateDirectory(path.dirname(deliveryFile));
  for (const file of [sessionFile, deliveryFile])
    await fs.writeFile(file + ".lock", "", { mode: 0o600 });
  const script = `
    const fs = require('node:fs/promises'); const original = fs.open;
    const paths = JSON.parse(process.argv[1]);
    fs.open = async function(file, ...args) {
      if (paths.some(p => file.startsWith(p + '.') && file.endsWith('.tmp'))) {
        process.send('locked'); setInterval(() => {}, 1000); await new Promise(() => {});
      }
      return original.call(this, file, ...args);
    };
    const { updateJson } = require(${JSON.stringify(require.resolve("@traceai/agent-core/files"))});
    Promise.all(paths.map(p => updateJson(p, () => ({ ignored: true })))).catch(() => process.exit(2));
  `;
  const writer = spawn(
    process.execPath,
    ["-e", script, JSON.stringify([sessionFile, deliveryFile])],
    { stdio: ["ignore", "ignore", "ignore", "ipc"] },
  );
  const exited = new Promise((resolve) => writer.once("exit", resolve));
  t.after(async () => {
    writer.kill("SIGKILL");
    await exited;
  });
  await new Promise((resolve, reject) => {
    let locks = 0;
    const timer = setTimeout(
      () => reject(new Error("writer failed to acquire leases")),
      5000,
    );
    writer.on("message", () => {
      if (++locks === 2) {
        clearTimeout(timer);
        resolve();
      }
    });
    writer.once("error", reject);
  });
  writer.kill("SIGKILL");
  await exited;
  await hooks(f, prompt, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  const next = "after-crash";
  await fs.appendFile(f.transcript, await rowsFor(f, next, "RECOVERED"));
  await hooks(f, next, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  const all = spans(f.received);
  assert.equal(all.length, 8);
  assert.equal(all.filter((s) => !s.parentSpanId).length, 2);
  const delivery = await core.readJson(deliveryFile);
  assert.equal(delivery.exported, 8);
  assert.equal(delivery.dropped, 0);
  assert((await core.readJson(sessionFile)).completed.includes(next));
  for (const file of [sessionFile, deliveryFile])
    await assert.rejects(fs.stat(file + ".write-lock"), { code: "ENOENT" });
});

for (const interrupted of ["[Request interrupted by user]", "[Request interrupted by user for tool use]", false])
  test(`replacement prompt never lends success or output to unfinished prompt; interruption=${interrupted}`, async (t) => {
    const f = await fixture(t);
    const rows = (await readRows(f)).slice(0, -1);
    if (interrupted)
      rows.push({
        ...rows[0],
        message: {
          role: "user",
          content: [{ type: "text", text: interrupted }],
        },
      });
    await writeRows(f, rows);
    await hooks(f, prompt, ["UserPromptSubmit"]);
    const next = "after-interruption";
    await fs.appendFile(f.transcript, await rowsFor(f, next, "RECOVERED"));
    await hooks(f, next, [
      "UserPromptSubmit",
      "Stop",
      "SessionEnd",
      "SessionEnd",
    ]);
    const all = spans(f.received);
    const roots = all.filter((s) => !s.parentSpanId);
    assert.equal(roots.length, 2);
    const first = roots.find((s) =>
      attrs(s)["input.value"].includes("SAMPLE_USER_PROMPT"),
    );
    const second = roots.find((s) => s !== first);
    assert.equal(
      attrs(first)["fi.agent.outcome"],
      interrupted ? "interrupted" : "incomplete",
    );
    assert.equal(attrs(first)["output.value"], undefined);
    assert.equal(attrs(second)["fi.agent.outcome"], "success");
    assert.match(attrs(second)["output.value"], /RECOVERED_MODEL_OUTPUT/);
    assert.equal(new Set(all.map((s) => s.spanId)).size, all.length);
    assert.equal(total(all, "gen_ai.usage.output_tokens"), 33);
  });

for (const expires of [false, true])
  test(`missing transcript gets bounded terminal settlement and recoverable expiry; expires=${expires}`, async (t) => {
    const f = await fixture(t);
    const contents = await fs.readFile(f.transcript, "utf8");
    await fs.unlink(f.transcript);
    await hooks(f, prompt, ["UserPromptSubmit"]);
    let created;
    if (!expires)
      created = new Promise((resolve, reject) =>
        setTimeout(
          () =>
            fs
              .writeFile(f.transcript, contents, { mode: 0o600 })
              .then(resolve, reject),
          500,
        ),
      );
    await handleHook({
      session_id: session,
      prompt_id: prompt,
      transcript_path: f.transcript,
      cwd: f.home,
      hook_event_name: "Stop",
      last_assistant_message: "SAMPLE_MODEL_OUTPUT: read complete.",
    });
    if (expires) {
      const pending = await core.readJson(statePath(f));
      assert.equal(pending.ended, false);
      assert(!pending.completed.includes(prompt));
      assert.equal(spans(f.received).length, 0);
      await fs.writeFile(f.transcript, contents, { mode: 0o600 });
    } else await created;
    await hooks(f, prompt, ["SessionEnd", "Stop", "SessionEnd"]);
    const all = spans(f.received);
    assert.equal(all.length, 4);
    assert.equal(all.filter((s) => !s.parentSpanId).length, 1);
    assert.equal(total(all, "gen_ai.usage.output_tokens"), 22);
  });

test("host disableAllHooks overrides historical activation without changing the host setting", async (t) => {
  const f = await fixture(t);
  const bin = path.join(f.home, "bin");
  await fs.mkdir(bin);
  await fs.writeFile(
    path.join(bin, "claude"),
    '#!/bin/sh\nprintf "2.1.318 (Claude Code)\\n"\n',
    { mode: 0o700 },
  );
  process.env.PATH = bin + path.delimiter + process.env.PATH;
  await adapter.installHooks(f.context);
  await hooks(f, prompt, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  const file = path.join(path.dirname(f.config.paths.user), "settings.json");
  await core.updateJson(file, (settings) => ({
    ...settings,
    disableAllHooks: true,
  }));
  for (const check of [false, true]) {
    const status = await core.inspectStatus({
      agent: "claude",
      home: f.home,
      env: f.env,
      adapter: adapter.claudeAdapter,
      check,
    });
    assert.equal(status.exitCode, 2);
    assert(status.issues.includes("HOST_HOOKS_DISABLED"));
    assert.equal(status.registration, "NOT_REGISTERED");
  }
  const setup = await core.setup({
    agent: "claude",
    home: f.home,
    env: f.env,
    adapter: adapter.claudeAdapter,
  });
  assert.equal(setup.registered, false);
  assert(setup.diagnostics.some((d) => d.code === "HOST_HOOKS_DISABLED"));
  assert.equal((await core.readJson(file)).disableAllHooks, true);
});

test("unified core redacts complete Basic auth headers in tool and generation history", async (t) => {
  const f = await fixture(t);
  const rows = await readRows(f);
  const secret = Buffer.from("synthetic-user:synthetic-password").toString(
    "base64",
  );
  rows[1].message.content[0].name = "Bash";
  rows[1].message.content[0].input = {
    command: `curl -H 'Authorization: Basic ${secret}' -H 'Proxy-Authorization: Basic ${secret}' https://example.invalid`,
  };
  await writeRows(f, rows);
  await hooks(f, prompt, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  assert.equal(spans(f.received).length, 4);
  assert(!JSON.stringify(f.received).includes(secret));
});

test("unified core refuses repository project changes with environment credentials", async (t) => {
  const f = await fixture(t);
  await core.setup({
    agent: "claude",
    home: f.home,
    env: f.env,
    configureOnly: true,
    projectName: "claude-test",
  });
  const repo = path.join(f.home, "repo");
  await fs.mkdir(path.join(repo, ".claude"), { recursive: true, mode: 0o700 });
  await fs.writeFile(
    path.join(repo, ".claude", "futureagi.json"),
    JSON.stringify({ schemaVersion: 1, projectName: "unapproved-project" }),
    { mode: 0o600 },
  );
  const env = { ...f.env };
  delete env.FI_PROJECT_NAME;
  delete env.FI_OTLP_ENDPOINT;
  const config = await core.resolveConfig({
    agent: "claude",
    home: f.home,
    projectDir: repo,
    env,
  });
  assert.equal(config.destinationApproved, false);
  assert(config.issues.includes("DESTINATION_NOT_APPROVED"));
  f.received.length = 0;
  await adapter.exportTranscriptTurn(
    config,
    f.state,
    f.transcript,
    session,
    "success",
  );
  assert.equal(f.received.length, 0);
});

test("a successful later provider response recovers earlier provider errors", async (t) => {
  const f = await fixture(t);
  const rows = await readRows(f);
  rows.splice(1, 0, {
    ...rows[0],
    type: "assistant",
    isApiErrorMessage: true,
    message: { role: "assistant", content: "synthetic failed attempt" },
  });
  await writeRows(f, rows);
  await hooks(f, prompt, ["UserPromptSubmit", "Stop", "SessionEnd"]);
  const all = spans(f.received);
  assert.equal(
    attrs(all.find((s) => !s.parentSpanId))["fi.agent.outcome"],
    "success",
  );
  assert.equal(all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 2);
});

for (const change of ["setup", "destination"])
  test(`pending source snapshots do not cross ${change} boundaries`, async (t) => {
    const f = await fixture(t);
    await core.setup({
      agent: "claude",
      home: f.home,
      env: f.env,
      configureOnly: true,
      projectName: "claude-test",
    });
    await hooks(f, prompt, ["UserPromptSubmit"]);
    f.received.length = 0;
    if (change === "setup") {
      await core.setup({
        agent: "claude",
        home: f.home,
        env: f.env,
        configureOnly: true,
        projectName: "claude-test",
      });
    } else process.env.FI_PROJECT_NAME = "new-approved-environment-project";
    f.received.length = 0;
    await hooks(f, prompt, ["Stop", "SessionEnd"]);
    assert.equal(spans(f.received).length, 0);
    const next = "after-fence";
    await fs.appendFile(f.transcript, await rowsFor(f, next, "ALLOWED"));
    await hooks(f, next, ["UserPromptSubmit", "Stop", "SessionEnd"]);
    assert.equal(spans(f.received).length, 4);
    assert(!JSON.stringify(f.received).includes("SAMPLE_USER_PROMPT"));
    assert(JSON.stringify(f.received).includes("ALLOWED_MODEL_OUTPUT"));
  });

test("unified core ignores unsafe and malformed repository layers for hooks", async (t) => {
  const f = await fixture(t);
  const repo = path.join(f.home, "external-drive", "repo");
  await fs.mkdir(path.join(repo, ".claude"), { recursive: true, mode: 0o700 });
  await fs.chmod(path.join(f.home, "external-drive"), 0o775);
  for (const malformed of [false, true]) {
    if (malformed) {
      await fs.chmod(path.join(f.home, "external-drive"), 0o700);
      await fs.writeFile(
        path.join(repo, ".claude", "futureagi.json"),
        "not json",
        { mode: 0o600 },
      );
    }
    const config = await core.resolveConfig({
      agent: "claude",
      home: f.home,
      projectDir: repo,
      env: f.env,
    });
    assert(config.issues.includes("PROJECT_CONFIG_IGNORED"));
    assert.equal(config.projectName, "claude-test");
    const id = malformed ? "malformed-project" : "external-project";
    await fs.appendFile(f.transcript, await rowsFor(f, id, "ALLOWED"));
    for (const hook_event_name of ["UserPromptSubmit", "Stop", "SessionEnd"])
      await handleHook({
        session_id: session,
        prompt_id: id,
        transcript_path: f.transcript,
        cwd: repo,
        hook_event_name,
      });
  }
  assert.equal(spans(f.received).length, 8);
});
