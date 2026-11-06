const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  fixture,
  payload,
  spans,
  child,
  core,
  fs,
  path,
} = require("./helpers.cjs");
const {
  installRuntime,
  register,
  commands,
} = require("../dist/registration.js");

test("daemon-free persistent hooks work with GUI-like PATH and export once at terminal boundary", async (t) => {
  const f = await fixture(t),
    runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const hooks = commands(runtime, f.config.paths.configDir),
    env = { HOME: f.home, PATH: "/path-not-present" };
  const latencies = [];
  for (const [event, data] of [
    [
      "beforeSubmitPrompt",
      { prompt: "synthetic-cursor-secret list files PROMPT_CAPTURE" },
    ],
    [
      "postToolUse",
      {
        tool_use_id: "read-1",
        tool_name: "Read",
        tool_input: { file: "README.md" },
        tool_output: "TOOL_CAPTURE",
      },
    ],
    ["afterAgentResponse", { text: "RESPONSE_CAPTURE" }],
  ]) {
    const result = await child(hooks[event].command, payload(event, data), env);
    assert.equal(result.code, 0);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
    latencies.push(result.ms);
    assert.equal(
      spans(f.requests).length,
      0,
      "Nonterminal hooks never wait on HTTP",
    );
  }
  const dir = path.join(f.config.paths.stateDir, "cursor-turns");
  const stateFiles = await fs.readdir(dir);
  assert.equal(stateFiles.length, 1);
  const saved = await fs.readFile(path.join(dir, stateFiles[0]), "utf8");
  assert.ok(
    !/PROMPT_CAPTURE|TOOL_CAPTURE|RESPONSE_CAPTURE|synthetic-cursor-secret/.test(
      saved,
    ),
  );
  assert.equal(
    (await fs.stat(path.join(dir, stateFiles[0]))).mode & 0o777,
    0o600,
  );
  for (let i = 0; i < 2; i++) {
    const stop = await child(
      hooks.stop.command,
      payload("stop", { status: "completed" }),
      env,
    );
    assert.equal(stop.code, 0);
    assert.equal(stop.stdout, "");
    assert.equal(stop.stderr, "");
  }
  const all = spans(f.requests),
    root = all.find((s) => !s.parentSpanId);
  assert.equal(all.length, 2);
  assert.equal(all.find((s) => s.parentSpanId).parentSpanId, root.spanId);
  assert.ok(
    !JSON.stringify(f.requests.map((r) => r.body)).includes(
      "synthetic-cursor-secret",
    ),
  );
  const final = JSON.parse(await fs.readFile(path.join(dir, stateFiles[0])));
  assert.equal(final.phase, "finalized");
  assert.equal(final.encrypted, undefined);
  assert.ok(
    !(await fs.readdir(f.config.paths.configDir)).includes(
      "cursor-worker.json",
    ),
  );
  console.log(
    JSON.stringify({
      evidence: "daemon-free-hook-local-otlp-not-real-ide",
      spanCount: all.length,
      hookLatencyMs: latencies,
    }),
  );
  await core.disable(f.options);
  const result = await child(
    hooks.beforeSubmitPrompt.command,
    payload("beforeSubmitPrompt", {
      generation_id: "disabled",
      prompt: "do not collect",
    }),
    env,
  );
  assert.equal(result.code, 0);
  assert.equal(result.stdout, "");
  assert.equal(spans(f.requests).length, 2);
});
test("malformed and oversize input, unknown version and kill switch are neutral failures", async (t) => {
  const f = await fixture(t),
    runtime = await installRuntime(f.context),
    hooks = commands(runtime, f.config.paths.configDir);
  for (const body of [
    "{bad",
    "x".repeat(300 * 1024),
    payload("stop", { cursor_version: "unknown", status: "completed" }),
  ]) {
    const result = await child(hooks.stop.command, body, {
      HOME: f.home,
      PATH: "/missing",
    });
    assert.equal(result.code, 0);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
  }
  const killed = await child(
    hooks.beforeSubmitPrompt.command,
    payload("beforeSubmitPrompt", { prompt: "secret" }),
    { HOME: f.home, PATH: "/missing", FI_AGENT_ENABLED: "false" },
  );
  assert.equal(killed.code, 0);
  assert.equal(spans(f.requests).length, 0);
});
