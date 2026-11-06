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
  const run = (args, vars = {}, cwd = home) =>
    new Promise((resolve) => {
      const child = spawn(process.execPath, [cli, ...args], {
        env: { ...env, ...vars }, cwd,
      });
      let stdout = "",
        stderr = "";
      child.stdout.on("data", (d) => (stdout += d));
      child.stderr.on("data", (d) => (stderr += d));
      child.on("exit", (code) => resolve({ code, stdout, stderr }));
      child.stdin.end();
    });
  return { home, run, env };
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
test("OpenCode status and doctor default to the plugin's current project config", async t => {
  const f = await fixture(t);
  const core = require('@traceai/agent-core');
  const { opencodeAdapter, OPENCODE_PACKAGE } = require('../dist/adapters/opencode.js');
  const projectDir = path.join(f.home, 'project');
  await fs.mkdir(projectDir);
  const options = { agent: 'opencode', home: f.home, env: f.env, projectDir };
  const paths = await core.configPaths(options);
  await core.writeProtected(paths.user, JSON.stringify({ schemaVersion: 1, captureContent: true }));
  await core.writeProtected(path.join(path.dirname(paths.user), 'opencode.json'), JSON.stringify({ plugins: [OPENCODE_PACKAGE] }));
  await core.writeProtected(paths.project, JSON.stringify({ schemaVersion: 1, captureContent: false }));
  await core.writeProtected(path.join(projectDir, 'opencode.json'), JSON.stringify({ plugins: ['-futureagi.tracing'] }));
  const pluginConfig = await core.resolveConfig(options);
  const inspected = await opencodeAdapter.inspect({ config: pluginConfig, scope: 'user', projectDir });
  for (const command of ['status', 'doctor']) {
    const implicit = JSON.parse((await f.run([command, 'opencode', '--json'], {}, projectDir)).stdout);
    const explicit = JSON.parse((await f.run([command, 'opencode', '--project-dir', projectDir, '--json'])).stdout);
    const relative = JSON.parse((await f.run([command, 'opencode', '--project-dir', '.', '--json'], {}, projectDir)).stdout);
    assert.deepEqual(implicit, explicit);
    assert.deepEqual(implicit, relative);
    assert.equal(implicit.privacy, pluginConfig.captureContent ? 'content-on-by-default' : 'structure-only');
    assert.equal(implicit.registration, inspected.registered ? 'REGISTERED' : 'NOT_REGISTERED');
    assert.equal(implicit.privacy, 'structure-only');
    assert.equal(implicit.registration, 'NOT_REGISTERED');
  }
  await fs.unlink(path.join(projectDir, 'opencode.json'));
  for (const args of [[], ['--project-dir', '.']]) {
    const active = JSON.parse((await f.run(['status', 'opencode', '--json', ...args], {}, projectDir)).stdout);
    assert.equal(active.registration, 'REGISTERED', 'relative project paths remain valid');
  }
});
for (const accepted of [true, false]) test(`masked saved-key recovery: replacement accepted=${accepted}`, async t => {
  const f = await fixture(t);
  let initial = true;
  const server = http.createServer((q, r) => {
    q.resume(); q.on('end', () => {
      r.statusCode = initial || (accepted && q.headers['x-api-key'] === 'synthetic-replacement-api' &&
        q.headers['x-secret-key'] === 'synthetic-replacement-secret') ? 200 : 401;
      r.setHeader('Content-Type', 'application/json'); r.end('{}');
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const setup = await f.run(['setup', 'opencode', '--project', 'coding-agents', '--configure-only', '--json'], {
    FI_API_KEY: 'synthetic-old-api', FI_SECRET_KEY: 'synthetic-old-secret',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  });
  assert.equal(setup.code, 0);
  initial = false;
  const headless = await f.run(['setup', 'opencode', '--project', 'coding-agents', '--configure-only', '--json']);
  assert.equal(JSON.parse(headless.stdout).code, 'AUTH_REJECTED');
  const result = await new Promise((resolve, reject) => {
    const child = spawn('python3', ['-I', path.join(__dirname, 'reauth-tty.py'), cli, accepted ? '0' : '2'], { env: f.env, cwd: f.home });
    let output = ''; child.stdout.on('data', b => output += b); child.stderr.on('data', b => output += b);
    child.on('error', reject); child.on('exit', code => resolve({ code, output }));
  });
  assert.equal(result.code, 0, result.output);
  const file = path.join(f.env.FI_AGENT_CONFIG_DIR, 'credentials.json');
  const saved = Object.values(JSON.parse(await fs.readFile(file)).profiles)[0];
  assert.equal(saved.apiKey, accepted ? 'synthetic-replacement-api' : 'synthetic-old-api');
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
});
