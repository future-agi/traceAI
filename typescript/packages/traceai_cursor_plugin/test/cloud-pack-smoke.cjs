// Executed in E2B after local tarball installation into an empty home/cache. No Cloud API calls.
const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const root = "/work/cloud-clean";
const requirePacked = createRequire(path.join(root, "package.json"));
const core = requirePacked("@traceai/agent-core");
const cloud = requirePacked("@traceai/cursor-plugin/cloud");
function execute(command, args, env, cwd, input) {
  return new Promise((resolve, reject) => {
    const p = spawn(command, args, { env, cwd });
    let stdout = "", stderr = "";
    p.stdout.on("data", (b) => stdout += b); p.stderr.on("data", (b) => stderr += b);
    p.on("error", reject); p.stdin.on("error", (e) => { if (e.code !== "EPIPE") reject(e); });
    p.on("close", (code) => resolve({ code, stdout, stderr }));
    p.stdin.end(input);
  });
}
async function main() {
  const home = path.join(root, "home"), projectDir = path.join(root, "repo");
  await fs.mkdir(home, { recursive: true, mode: 0o700 }); await fs.mkdir(projectDir);
  const requests = [];
  const server = http.createServer((q, r) => {
    let b = ""; q.on("data", (x) => b += x); q.on("end", () => {
      assert.equal(q.url, "/tracer/v1/traces");
      assert.equal(q.headers["x-project-name"], "cloud-pack");
      requests.push(JSON.parse(b)); r.setHeader("Content-Type", "application/json"); r.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    const env = {
      HOME: home, PATH: "/unavailable", FI_CURSOR_CLOUD: "true",
      FI_API_KEY: "dummy-pack-api", FI_SECRET_KEY: "dummy-pack-secret",
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, FI_PROJECT_NAME: "cloud-pack",
    };
    const cli = path.join(root, "node_modules/@traceai/agents/dist/cli.js");
    const nativeBin = await execute(path.join(root, "node_modules/.bin/fi-agent"), ["--help"], { ...env, PATH: path.dirname(process.execPath) }, projectDir);
    assert.equal(nativeBin.code, 0); assert.match(nativeBin.stdout, /setup/);
    const setup = await execute(process.execPath, [cli, "setup", "cursor-cloud", "--project", "cloud-pack", "--json"], env, projectDir);
    assert.equal(setup.code, 2); assert.equal(setup.stderr, "");
    assert.equal(JSON.parse(setup.stdout).code, "UNSUPPORTED_HOST_VERSION");
    assert.equal(JSON.parse(setup.stdout).inspection.detail, "AUTHORIZED_CLOUD_RUN_REQUIRED");
    const options = { agent: "cursor-cloud", home, env, projectDir };
    const config = await core.resolveConfig(options);
    const context = { config, scope: "project", projectDir };
    await assert.rejects(fs.stat(config.paths.credentials), { code: "ENOENT" });
    await assert.rejects(fs.stat(path.join(projectDir, ".cursor/hooks.json")), { code: "ENOENT" });
    await fs.writeFile(path.join(projectDir, ".cursor/environment.json"), '{"install":"npm ci"}\n');
    await cloud.prepareCloudEnvironment(context, home);
    assert.equal(await fs.readFile(path.join(projectDir, ".cursor/environment.json"), "utf8"), '{"install":"npm ci"}\n');
    const hooks = JSON.parse(await fs.readFile(path.join(projectDir, ".cursor/hooks.json"))).hooks;
    for (const [event, fields] of [
      ["beforeSubmitPrompt", { prompt: "Show package tracing" }],
      ["postToolUseFailure", { tool_use_id: "source-1", tool_name: "Shell", tool_input: { command: "false" }, error_message: "intentional fixture failure" }],
      ["stop", { status: "error" }],
    ]) {
      const input = JSON.stringify({ hook_event_name: event, cursor_version: "3.24.18", conversation_id: "conversation-1", generation_id: "turn-1", ...fields });
      const result = await execute("/bin/sh", ["-c", hooks[event][0].command], env, projectDir, input);
      assert.deepEqual(result, { code: 1, stdout: "", stderr: "" });
    }
    const spans = requests.flatMap((r) => (r.resourceSpans ?? []).flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)));
    assert.equal(spans.length, 2); assert.equal(spans.filter((s) => !s.parentSpanId).length, 1);
    const rootSpan = spans.find((s) => !s.parentSpanId);
    assert.equal(rootSpan.attributes.find((a) => a.key === "fi.agent.outcome").value.stringValue, "error");
    const status = await execute(process.execPath, [cli, "status", "cursor-cloud", "--json", "--check"], env, projectDir);
    const state = JSON.parse(status.stdout);
    assert.equal(status.code, 2); assert.equal(state.registration, "REGISTERED");
    assert.equal(state.activation, "OBSERVED"); assert.equal(state.credentialCheck, "ACCEPTED");
    assert.equal(state.productReadback, "unverified");
    assert.match(state.qualification, /AUTHORIZED_CLOUD_RUN/);
    assert.ok(!JSON.stringify(state).includes("dummy-pack"));
    await execute(process.execPath, [cli, "uninstall", "cursor-cloud", "--json"], env, projectDir);
    await assert.rejects(fs.stat(path.join(projectDir, ".cursor/hooks.json")), { code: "ENOENT" });
    const evidence = {
      test: "cursor-cloud-local-tarball-contract", passed: true,
      node: process.version, publicRegistryInstall: false, realCloudAgent: false,
      spans: { roots: 1, tools: 1, modelRequests: 0, rootOutcome: "error" },
      status: { registration: state.registration, activation: state.activation, productReadback: state.productReadback, qualification: state.qualification },
      requires: ["eligible Cloud account", "repository authorization", "dashboard secrets", "real hosted hook ordering and trust verification", "real Future AGI media"],
    };
    await fs.writeFile("/work/cloud-pack-evidence.json", JSON.stringify(evidence, null, 2) + "\n");
    console.log(JSON.stringify(evidence));
  } finally { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
