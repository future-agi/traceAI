// Contract tests only. Running the packaged hook is NOT running Cursor Cloud orchestration.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const os = require("node:os");
const { spawn } = require("node:child_process");
const { core, fs, path, payload, spans, attributes } = require("./helpers.cjs");
const cloud = require("../dist/cloud.js");
async function fixture(t, configured = true) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-cloud-contract-"));
  const projectDir = path.join(home, "repo");
  await fs.mkdir(projectDir);
  const requests = [];
  let response = 200;
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (b) => body += b);
    req.on("end", () => {
      requests.push({ headers: req.headers, url: req.url, body: JSON.parse(body) });
      res.writeHead(response, { "Content-Type": "application/json" });
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => { server.closeAllConnections(); await new Promise((r) => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  const env = {
    HOME: home, PATH: "/missing", FI_CURSOR_CLOUD: "true",
    FI_API_KEY: "dummy-cloud-api", FI_SECRET_KEY: "dummy-cloud-secret",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    FI_PROJECT_NAME: "cloud-contract",
  };
  const options = { agent: "cursor-cloud", home, env, projectDir };
  // A poisonous saved file proves setup/status never open the developer's credential store.
  const paths = await core.configPaths(options);
  await core.ensurePrivateDirectory(paths.configDir);
  await fs.symlink(path.join(home, "never-read-credentials"), paths.credentials);
  if (configured) {
    const result = await core.setup({ ...options, scope: "project", configureOnly: true });
    assert.equal(result.configured, true);
  }
  const config = await core.resolveConfig(options);
  const context = { config, scope: "project", projectDir };
  return { home, projectDir, env, options, config, context, paths, requests, setResponse: (n) => response = n };
}
function run(command, input, f, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const p = spawn("/bin/sh", ["-c", command], { cwd: f.projectDir, env: { ...f.env, ...extraEnv } });
    let stdout = "", stderr = "";
    p.stdout.on("data", (c) => stdout += c); p.stderr.on("data", (c) => stderr += c);
    p.on("error", reject); p.stdin.on("error", (e) => { if (e.code !== "EPIPE") reject(e); });
    p.on("close", (code) => resolve({ code, stdout, stderr, ms: Date.now() - start }));
    p.stdin.end(typeof input === "string" ? input : JSON.stringify(input));
  });
}
async function hooks(f) {
  const result = await cloud.prepareCloudEnvironment(f.context, f.home);
  assert.equal(result.registered, true, JSON.stringify(result));
  return require("jsonc-parser").parse(await fs.readFile(path.join(f.projectDir, ".cursor/hooks.json"), "utf8")).hooks;
}
test("Cloud setup is environment-only, private nonsecret project config, no IDE inheritance", async (t) => {
  const f = await fixture(t);
  assert.equal(f.config.enabled, true);
  assert.equal(f.config.captureContent, true);
  const saved = await fs.readFile(f.paths.project, "utf8");
  assert.ok(!/dummy-cloud|apiKey|secretKey|profile/.test(saved));
  assert.equal((await fs.stat(f.paths.project)).mode & 0o777, 0o600);
  assert.equal((await fs.lstat(f.paths.credentials)).isSymbolicLink(), true);
  const absent = await core.resolveConfig({ ...f.options, env: { HOME: f.home } });
  assert.equal(absent.enabled, false); assert.equal(absent.credentials, undefined);
  assert.ok(absent.issues.includes("NOT_CONFIGURED"));
  const status = await core.inspectStatus({ ...f.options, check: true, adapter: cloud.cursorCloudAdapter });
  assert.equal(status.credentialCheck, "ACCEPTED"); assert.equal(status.registration, "NOT_REGISTERED");
  assert.ok(!JSON.stringify(status).includes("dummy-cloud"));
  await assert.rejects(core.resolveConfig({ ...f.options, env: { ...f.env, FI_SECRET_KEY: undefined } }), /INCOMPLETE_CREDENTIAL/);
  await assert.rejects(core.setup({ ...f.options, scope: "user" }), /CLOUD_PROJECT_SCOPE/);
  await core.setup({ ...f.options, scope: "project", configureOnly: true, captureContent: false });
  await core.updateJson(f.paths.project, (old) => ({ ...old, captureContent: true }));
  assert.equal((await core.resolveConfig(f.options)).captureContent, false, "repository cannot raise the approved ceiling");
  await core.setup({ ...f.options, scope: "project", configureOnly: true });
  assert.equal((await core.resolveConfig(f.options)).captureContent, false, "repeat setup preserves opt-out");
  assert.equal((await fs.stat(f.paths.user)).mode & 0o777, 0o600);
  assert.ok(!/dummy-cloud|apiKey|secretKey|profile/.test(await fs.readFile(f.paths.user, "utf8")));
  await core.updateJson(f.paths.project, (old) => ({ ...old, transport: { endpoint: "https://unapproved.example/traces" } }));
  const envWithoutDestination = { ...f.env, FI_OTLP_ENDPOINT: undefined };
  const rejected = await core.resolveConfig({ ...f.options, env: envWithoutDestination });
  assert.equal(rejected.enabled, false); assert.ok(rejected.issues.includes("DESTINATION_NOT_APPROVED"));
});
test("invalid keys create no configuration or hooks and lifecycle stays held", async (t) => {
  const f = await fixture(t, false);
  f.setResponse(401);
  const result = await core.setup({ ...f.options, scope: "project", adapter: cloud.cursorCloudAdapter });
  assert.equal(result.configured, false);
  await assert.rejects(fs.stat(f.paths.project), { code: "ENOENT" });
  await assert.rejects(fs.stat(path.join(f.projectDir, ".cursor/hooks.json")), { code: "ENOENT" });
  assert.equal((await cloud.cursorCloudAdapter.detect(f.context)).supported, false);
  await assert.rejects(cloud.cursorCloudAdapter.install(f.context), /AUTHORIZED_CLOUD_RUN_REQUIRED/);
  assert.equal(cloud.cloudQualification.live, false);
});
test("project hooks preserve comments and unrelated entries, repeat, refuse foreign tracing, uninstall only owned", async (t) => {
  const f = await fixture(t);
  const file = path.join(f.projectDir, ".cursor/hooks.json");
  await fs.writeFile(file, '{\n // user comment\n "version":1,"hooks":{"stop":[{"command":"echo keep"}],"workspaceOpen":[{"command":"echo editor-only"}]}\n}', { mode: 0o644 });
  await hooks(f); await cloud.prepareCloudEnvironment(f.context, f.home);
  const raw = await fs.readFile(file, "utf8");
  assert.match(raw, /user comment/);
  const { parse } = require("jsonc-parser");
  const doc = parse(raw);
  assert.equal(doc.hooks.stop.length, 2); assert.equal(doc.hooks.sessionEnd, undefined);
  assert.equal(doc.hooks.beforeShellExecution, undefined, "generic tool family avoids duplicate shell spans");
  for (const event of cloud.CLOUD_EVENTS) {
    const command = doc.hooks[event].find((h) => h.command.includes("fi-cursor-cloud-v1")).command;
    assert.ok(!command.includes(f.home)); assert.ok(!command.includes("npx"));
  }
  await cloud.removeCloudHooks(f.context);
  const remaining = parse(await fs.readFile(file, "utf8"));
  assert.deepEqual(remaining.hooks.stop, [{ command: "echo keep" }]);
  assert.deepEqual(remaining.hooks.workspaceOpen, [{ command: "echo editor-only" }]);
  await fs.writeFile(file, '{"version":1,"hooks":{"stop":[{"command":"fi-cursor-hook"}]}}');
  await assert.rejects(cloud.prepareCloudEnvironment(f.context, f.home), /DUPLICATE_REGISTRATION/);
});
test("packaged hook exports two source-anchored traces in one session, no model spans, encrypted state, terminal dedup", async (t) => {
  const f = await fixture(t), installed = await hooks(f), latencies = [];
  for (const [event, data] of [
    ["beforeSubmitPrompt", { prompt: "PROMPT_CAPTURE dummy-cloud-secret", user_email: "never-export@example.test" }],
    ["postToolUse", { tool_name: "Read", tool_use_id: "read-1", tool_input: { file: "hello.txt" }, tool_output: "TOOL_CAPTURE" }],
    ["afterAgentResponse", { text: "OUTPUT_CAPTURE" }],
  ]) {
    const result = await run(installed[event][0].command, payload(event, data), f);
    assert.deepEqual([result.code, result.stdout, result.stderr], [1, "", ""]);
    latencies.push(result.ms); assert.equal(spans(f.requests).length, 0);
  }
  const stateDir = path.join(f.paths.stateDir, "cursor-cloud-turns");
  const files = await fs.readdir(stateDir);
  assert.equal(files.length, 1);
  const encrypted = await fs.readFile(path.join(stateDir, files[0]), "utf8");
  assert.ok(!/PROMPT_CAPTURE|TOOL_CAPTURE|OUTPUT_CAPTURE|dummy-cloud/.test(encrypted));
  assert.equal((await fs.stat(path.join(stateDir, files[0]))).mode & 0o777, 0o600);
  const stop = payload("stop", { status: "completed", input_tokens: 900, output_tokens: 500 });
  for (let n = 0; n < 2; n++) await run(installed.stop[0].command, stop, f);
  assert.equal(spans(f.requests).length, 2);
  // Early read-only prompt absent: a reduced run, never a fabricated prompt or model generation.
  await run(installed.postToolUse[0].command, payload("postToolUse", { generation_id: "turn-2", tool_name: "MCP:search", tool_use_id: "mcp-1", tool_input: { query: "x" }, tool_output: '{"redacted":true}' }), f);
  await run(installed.stop[0].command, payload("stop", { generation_id: "turn-2", status: "completed" }), f);
  const all = spans(f.requests), roots = all.filter((s) => !s.parentSpanId), tools = all.filter((s) => s.parentSpanId);
  assert.equal(all.length, 4); assert.equal(roots.length, 2); assert.equal(tools.length, 2);
  assert.equal(new Set(roots.map((s) => attributes(s)["session.id"])).size, 1);
  assert.equal(new Set(roots.map((s) => s.traceId)).size, 2);
  for (const tool of tools) assert.ok(roots.some((r) => r.traceId === tool.traceId && r.spanId === tool.parentSpanId));
  const runRoot = roots.find((s) => attributes(s)["fi.agent.unit"] === "run");
  assert.ok(runRoot); assert.equal(attributes(runRoot)["input.value"], undefined);
  assert.match(attributes(runRoot)["fi.agent.gaps"], /PROMPT_NOT_OBSERVED/);
  assert.equal(attributes(tools.find((s) => s.name === "MCP"))["output.value"], undefined);
  for (const span of all) {
    const attrs = attributes(span);
    assert.notEqual(attrs["fi.span.kind"], "LLM");
    assert.ok(!Object.keys(attrs).some((k) => /token_count|cost/.test(k)));
  }
  const wire = JSON.stringify(f.requests.map((r) => r.body));
  assert.ok(!/dummy-cloud|never-export@example/.test(wire));
  assert.match(wire, /PROMPT_CAPTURE/); assert.match(wire, /OUTPUT_CAPTURE/);
  const finalState = JSON.parse(await fs.readFile(path.join(stateDir, files[0])));
  assert.equal(finalState.phase, "finalized"); assert.equal(finalState.encrypted, undefined);
  console.log(JSON.stringify({ evidence: "cursor-cloud-packaged-hook-contract-only", realCloudAgent: false, roots: roots.length, tools: tools.length, modelSpans: 0, nonterminalLatencyMs: latencies }));
});
test("kill switch, missing Cloud opt-in and malformed inputs never collect or produce policy output", async (t) => {
  const f = await fixture(t), installed = await hooks(f);
  for (const env of [{ FI_CURSOR_CLOUD: undefined }, { FI_AGENT_ENABLED: "false" }]) {
    const result = await run(installed.beforeSubmitPrompt[0].command, payload("beforeSubmitPrompt", { prompt: "NO_CAPTURE" }), f, env);
    assert.deepEqual([result.code, result.stdout, result.stderr], [1, "", ""]);
  }
  for (const input of ["{bad", "x".repeat(300 * 1024), payload("stop", { cursor_version: "invalid" }), payload("sessionEnd")]) {
    const result = await run(installed.stop[0].command, input, f);
    assert.deepEqual([result.code, result.stdout, result.stderr], [1, "", ""]);
  }
  await core.disable(f.options);
  await run(installed.stop[0].command, payload("stop", { status: "completed" }), f);
  assert.equal(spans(f.requests).length, 0);
  await assert.rejects(fs.stat(path.join(f.paths.stateDir, "cursor-cloud-turns")), { code: "ENOENT" });
});
test("content opt-out drops prompt, tool and response text but keeps tool failures", async (t) => {
  const f = await fixture(t), installed = await hooks(f);
  for (const [event, data] of [
    ["beforeSubmitPrompt", { prompt: "PRIVATE_PROMPT" }],
    ["postToolUseFailure", { tool_name: "Shell", tool_use_id: "failure-1", tool_input: { command: "PRIVATE_INPUT" }, error_message: "PRIVATE_FAILURE" }],
    ["afterAgentResponse", { text: "PRIVATE_OUTPUT" }],
    ["stop", { status: "completed" }],
  ]) await run(installed[event][0].command, payload(event, data), f, { FI_AGENT_CAPTURE_CONTENT: "false" });
  const all = spans(f.requests);
  assert.equal(all.length, 2);
  assert.equal(attributes(all.find((s) => s.parentSpanId))["fi.agent.outcome"], "error");
  assert.ok(!JSON.stringify(f.requests.map((r) => r.body)).includes("PRIVATE_"));
});

test("Cloud cleanup removes unchanged created hooks and receipt, retaining shared runtime", async (t) => {
  const f = await fixture(t);
  await hooks(f);
  await cloud.prepareCloudEnvironment(f.context, f.home);
  await cloud.removeCloudHooks(f.context);
  await assert.rejects(fs.stat(path.join(f.projectDir, ".cursor/hooks.json")), { code: "ENOENT" });
  assert.equal((await cloud.inspectCloud(f.context)).registered, false);
  await cloud.removeCloudHooks(f.context);
});

test("held Cloud setup retains actionable diagnostics through the unified lifecycle", async (t) => {
  const f = await fixture(t);
  const result = await core.setup({ ...f.options, scope: "project", adapter: cloud.cursorCloudAdapter });
  assert.equal(result.code, "UNSUPPORTED_HOST_VERSION");
  assert.equal(result.inspection.detail, "AUTHORIZED_CLOUD_RUN_REQUIRED");
  assert.ok(result.diagnostics.some((item) => item.code === "AUTHORIZED_CLOUD_RUN_REQUIRED"));
});
