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

test("repeated project setup preserves its profile, route and privacy without environment overrides", async (t) => {
  const f = await fixture(t);
  const received = [];
  const servers = [0, 1].map((id) =>
    http.createServer((q, r) => {
      q.resume();
      q.on("end", () => {
        received.push({
          id,
          project: q.headers["x-project-name"],
          api: q.headers["x-api-key"],
        });
        r.setHeader("Content-Type", "application/json");
        r.end("{}");
      });
    }),
  );
  for (const s of servers) {
    await new Promise((r) => s.listen(0, "127.0.0.1", r));
    t.after(() => new Promise((r) => s.close(r)));
  }
  const project = path.join(f.home, "repository");
  await fs.mkdir(project);
  const args = [
    "setup",
    "copilot",
    "--project-dir",
    project,
    "--configure-only",
    "--json",
  ];
  for (const [id, name, scope] of [
    [0, "global-project", "user"],
    [1, "project-special", "project"],
  ]) {
    const result = await f.run(
      [
        ...args,
        "--scope",
        scope,
        "--project",
        name,
        ...(id ? ["--no-capture-content"] : []),
      ],
      {
        FI_API_KEY: "synthetic-api-" + id,
        FI_SECRET_KEY: "synthetic-secret-" + id,
        FI_OTLP_ENDPOINT: `http://127.0.0.1:${servers[id].address().port}/tracer/v1/traces`,
      },
    );
    assert.equal(result.code, 0, result.stdout + result.stderr);
  }
  const settingsPath = path.join(project, ".copilot/futureagi.json");
  const before = JSON.parse(await fs.readFile(settingsPath));
  const again = await f.run([...args, "--scope", "project"]);
  assert.equal(again.code, 0, again.stdout + again.stderr);
  const after = JSON.parse(await fs.readFile(settingsPath));
  assert.deepEqual(after, before);
  assert.deepEqual(received.at(-1), {
    id: 1,
    project: "project-special",
    api: "synthetic-api-1",
  });
  const status = JSON.parse(
    (await f.run(["status", "copilot", "--project-dir", project, "--json"]))
      .stdout,
  );
  assert.equal(status.projectName, "project-special");
  assert.equal(status.privacy, "structure-only");
  assert.equal(status.destination, before.transport.endpoint);
});
test("uninstall fences first and removes only owned registration without reading malformed configuration", async (t) => {
  const f = await fixture(t);
  const root = path.join(f.home, ".config/futureagi/agents");
  await fs.mkdir(root, { recursive: true, mode: 0o700 });
  await fs.writeFile(path.join(root, "credentials.json"), "{broken json", {
    mode: 0o600,
  });
  await fs.mkdir(path.join(f.home, ".copilot"), { mode: 0o700 });
  await fs.writeFile(
    path.join(f.home, ".copilot/futureagi.json"),
    "{broken json",
    { mode: 0o600 },
  );
  const registration = path.join(root, "copilot-launcher.json");
  const args = [
    "uninstall",
    "copilot",
    "--project-dir",
    path.join(f.home, "missing-project"),
    "--json",
  ];
  await fs.writeFile(
    registration,
    JSON.stringify({ version: "0.1.0", mechanism: "native-pipe" }),
    { mode: 0o600 },
  );
  const result = await f.run(args);
  assert.equal(result.code, 0, result.stdout + result.stderr);
  await assert.rejects(fs.stat(registration), { code: "ENOENT" });
  assert.equal(
    JSON.parse(await fs.readFile(path.join(root, "installation.json"))).disabled
      .copilot,
    true,
  );
  assert.equal(
    await fs.readFile(path.join(root, "credentials.json"), "utf8"),
    "{broken json",
  );
  await fs.writeFile(
    registration,
    JSON.stringify({ version: "foreign", mechanism: "not-owned" }),
    { mode: 0o600 },
  );
  assert.equal((await f.run(args)).code, 0);
  assert.equal(JSON.parse(await fs.readFile(registration)).version, "foreign");
  await fs.rm(registration);
  const target = path.join(f.home, "foreign.json");
  await fs.writeFile(
    target,
    JSON.stringify({ version: "0.1.0", mechanism: "native-pipe" }),
    { mode: 0o600 },
  );
  await fs.symlink(target, registration);
  assert.equal((await f.run(args)).code, 2);
  assert.ok((await fs.lstat(registration)).isSymbolicLink());
  assert.ok((await fs.stat(target)).isFile());
  assert.equal(
    JSON.parse(await fs.readFile(path.join(root, "installation.json"))).disabled
      .copilot,
    true,
  );
});

test("credential replacement is explicit, actionable headlessly and transactional", async (t) => {
  const f = await fixture(t);
  let accepted = "old-synthetic-api";
  const server = http.createServer((q, r) => {
    q.resume();
    q.on("end", () => {
      r.statusCode = q.headers["x-api-key"] === accepted ? 200 : 401;
      r.setHeader("Content-Type", "application/json");
      r.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const args = [
    "setup",
    "copilot",
    "--project-dir",
    f.home,
    "--configure-only",
    "--json",
  ];
  const vars = {
    FI_API_KEY: accepted,
    FI_SECRET_KEY: "old-synthetic-secret",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  };
  assert.equal((await f.run(args, vars)).code, 0);
  const file = path.join(f.home, ".config/futureagi/agents/credentials.json");
  const before = await fs.readFile(file, "utf8");
  accepted = "new-synthetic-api";
  const rejected = await f.run(args);
  assert.equal(rejected.code, 2);
  assert.equal(JSON.parse(rejected.stdout).code, "AUTH_REJECTED");
  assert.match(JSON.parse(rejected.stdout).detail, /--replace-credentials/);
  const missing = await f.run([...args, "--replace-credentials"]);
  assert.equal(
    JSON.parse(missing.stdout).code,
    "HEADLESS_CREDENTIALS_REQUIRED",
  );
  assert.match(missing.stdout, /FI_API_KEY/);
  assert.equal(await fs.readFile(file, "utf8"), before);
  const failed = await f.run([...args, "--replace-credentials"], vars);
  assert.equal(failed.code, 2);
  assert.equal(await fs.readFile(file, "utf8"), before);
  const replaced = await f.run([...args, "--replace-credentials"], {
    FI_API_KEY: accepted,
    FI_SECRET_KEY: "new-synthetic-secret",
  });
  assert.equal(replaced.code, 0, replaced.stdout + replaced.stderr);
  assert.ok(!(replaced.stdout + replaced.stderr).includes("new-synthetic"));
  const store = JSON.parse(await fs.readFile(file));
  assert.equal(store.profiles[store.defaultProfile].apiKey, accepted);
});
