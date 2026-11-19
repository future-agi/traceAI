const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { spawn } = require("node:child_process");
const http = require("node:http");
const cli = path.resolve(__dirname, "../dist/cli.js");
async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-cli-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    FI_AGENT_CONFIG_DIR: path.join(home, ".config/futureagi/agents"),
    XDG_STATE_HOME: path.join(home, ".local/state"),
    PI_CODING_AGENT_DIR: path.join(home, ".pi/agent"),
  };
  const run = (args, vars = {}) =>
    new Promise((resolve) => {
      const child = spawn(process.execPath, [cli, ...args], {
        env: { ...env, ...vars },
      });
      let stdout = "",
        stderr = "";
      child.stdout.on("data", (d) => (stdout += d));
      child.stderr.on("data", (d) => (stderr += d));
      child.on("exit", (code) => resolve({ code, stdout, stderr }));
      child.stdin.end();
    });
  return { home, run };
}
test("unqualified GUI is listed and blocked before config discovery, credentials or network", async (t) => {
  const f = await fixture(t);
  let calls = 0;
  const server = http.createServer((_q, r) => {
    calls++;
    r.setHeader("Content-Type", "application/json");
    r.end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const all = await f.run(["status", "--json"]);
  const listed = JSON.parse(all.stdout).agents.find((item) => item.agent === "antigravity-ide");
  assert.equal(listed.qualification, "ANTIGRAVITY_GUI_QUALIFICATION_REQUIRED");
  for (const args of [
    ["setup", "antigravity-ide", "--json"],
    ["setup", "antigravity-ide", "--configure-only", "--json"],
    ["setup", "antigravity-ide", "--scope", "project", "--project-dir", f.home, "--json"],
    ["status", "antigravity-ide", "--check", "--json"],
    ["doctor", "antigravity-ide", "--json"],
    ["enable", "antigravity-ide", "--json"],
    ["update", "antigravity-ide", "--json"],
    ["uninstall", "antigravity-ide", "--json"],
    ["run", "antigravity-ide", "--json", "--", "agy"],
  ]) {
    const result = await f.run(args, {
      FI_API_KEY: "synthetic-private-api",
      FI_SECRET_KEY: "synthetic-private-secret",
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    });
    assert.equal(result.code, 2);
    const report = JSON.parse(result.stdout);
    assert.equal(report.code, "ANTIGRAVITY_GUI_QUALIFICATION_REQUIRED");
    assert.equal(report.registration, "NOT_REGISTERED");
    assert.equal(report.activation, "NOT_OBSERVED");
    assert.equal(report.credentialCheck, "NOT_ATTEMPTED");
    assert.ok(!result.stdout.includes("synthetic-private"));
    assert.equal(result.stderr, "");
  }
  assert.equal(calls, 0);
  assert.deepEqual(await fs.readdir(f.home), []);
  const missing = await f.run(["setup", "antigravity-ide", "--json"]);
  assert.equal(JSON.parse(missing.stdout).code, "ANTIGRAVITY_GUI_QUALIFICATION_REQUIRED");
  const disabled = await f.run(["disable", "antigravity-ide", "--json"]);
  assert.equal(disabled.code, 0);
  const state = JSON.parse(await fs.readFile(path.join(f.home, ".config/futureagi/agents/installation.json")));
  assert.equal(state.disabled["antigravity-ide"], true);
  assert.equal(state.disabled.antigravity, undefined);
});
