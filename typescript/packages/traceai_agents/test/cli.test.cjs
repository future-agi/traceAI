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
test("CLI help names no secret flags; rejected secret argv never echoed", async (t) => {
  const f = await fixture(t);
  const help = await f.run(["--help"]);
  assert.equal(help.code, 0);
  assert.match(help.stdout, /--no-capture-content/);
  const bad = await f.run(["setup", "pi", "--secret-key", "never-print-me"]);
  assert.equal(bad.code, 2);
  assert.ok(!(bad.stdout + bad.stderr).includes("never-print-me"));
});
test("fresh status and disable work offline, JSON status never asserts connected", async (t) => {
  const f = await fixture(t);
  const before = await f.run(["status", "pi", "--json"]);
  assert.equal(before.code, 2);
  assert.equal(JSON.parse(before.stdout).credentials, "missing");
  const disabled = await f.run(["disable", "pi", "--json"]);
  assert.equal(disabled.code, 0);
  assert.equal(JSON.parse(disabled.stdout).code, "DISABLED");
  const state = JSON.parse(
    await fs.readFile(
      path.join(f.home, ".config/futureagi/agents/installation.json"),
    ),
  );
  assert.equal(state.disabled.pi, true);
});
test("headless setup validates, omits keys from output/project, persists opt-out, does not claim absent adapter", async (t) => {
  const f = await fixture(t);
  let calls = 0;
  const server = http.createServer((q, r) => {
    let data = "";
    q.on("data", (d) => (data += d));
    q.on("end", () => {
      calls++;
      assert.deepEqual(JSON.parse(data), { resourceSpans: [] });
      assert.equal(q.headers["x-project-name"], "cli-project");
      r.setHeader("Content-Type", "application/json");
      r.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const vars = {
    FI_API_KEY: "dummy-api-private",
    FI_SECRET_KEY: "dummy-secret-private",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  };
  const configured = await f.run(
    [
      "setup",
      "pi",
      "--project",
      "cli-project",
      "--no-capture-content",
      "--configure-only",
      "--json",
    ],
    vars,
  );
  assert.equal(configured.code, 0, configured.stderr);
  assert.equal(JSON.parse(configured.stdout).registered, false);
  assert.ok(
    !(configured.stdout + configured.stderr).includes("dummy-secret-private"),
  );
  const user = JSON.parse(
    await fs.readFile(path.join(f.home, ".pi/agent/futureagi.json")),
  );
  assert.equal(user.captureContent, false);
  assert.ok(!JSON.stringify(user).includes("dummy-"));
  const absent = await f.run(
    ["setup", "pi", "--project", "cli-project", "--json"],
    vars,
  );
  assert.equal(absent.code, 2);
  assert.equal(JSON.parse(absent.stdout).code, "ADAPTER_NOT_AVAILABLE");
  assert.equal(calls, 2);
});
