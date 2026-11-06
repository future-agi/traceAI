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
        env: { ...env, ...vars },
        cwd,
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
test("headless setup validates, omits keys from output/project, persists opt-out, reports the absent Pi host", async (t) => {
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
  // With the real Pi adapter wired in, a missing host is an installation failure
  // that names the missing executable, never a registered integration.
  const absentResult = JSON.parse(absent.stdout);
  assert.equal(absentResult.registered, false);
  assert.ok(
    absentResult.code === "UNSUPPORTED_HOST_VERSION" ||
      (absentResult.code === "INSTALLATION_FAILED" && absent.stdout.includes("PI_NOT_FOUND")),
    absent.stdout,
  );
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

test("Hermes status and doctor default to cwd; explicit project wins and Python agrees", async (t) => {
  const f = await fixture(t);
  const project = path.join(f.home, "workspace");
  const host = path.join(f.home, "hermes-profile");
  const bin = path.join(f.home, "bin");
  const write = async (file, value) => {
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await fs.writeFile(file, JSON.stringify(value), { mode: 0o600 });
  };
  let checks = 0;
  const server = http.createServer((q, r) => {
    q.resume();
    q.on("end", () => {
      checks++;
      assert.equal(q.headers["x-project-name"], "project-only");
      r.setHeader("Content-Type", "application/json");
      r.end("{}");
    });
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise(r => server.close(r)));
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  await fs.mkdir(project, { recursive: true });
  const { createHash } = require("node:crypto");
  const profile = { apiKey: "synthetic-api", secretKey: "synthetic-secret", endpoint,
    projectName: "global", captureContent: true, validatedAt: new Date().toISOString(),
    identityKey: "a".repeat(64), userId: "test-user" };
  await write(path.join(f.env.FI_AGENT_CONFIG_DIR, "credentials.json"), {
    schemaVersion: 1, defaultProfile: "global", profiles: {
      global: profile, project: { ...profile, projectName: "project-only", captureContent: false,
        projectBinding: createHash("sha256").update(await fs.realpath(project)).digest("hex") },
    },
  });
  await write(path.join(host, "futureagi.json"), {
    schemaVersion: 1, profile: "global", enabled: true, captureContent: true, projectName: "global",
  });
  await write(path.join(project, ".futureagi/agents.json"), { hermes: {
    schemaVersion: 1, profile: "project", enabled: true, captureContent: false, projectName: "project-only",
  } });
  await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, "hermes"), `#!${process.execPath}\nconst a=process.argv.slice(2);console.log(a.includes('--version')?'test-host':JSON.stringify([{name:'futureagi',version:'0.1.1',status:'enabled'}]));\n`, { mode: 0o700 });
  const vars = { HERMES_HOME: host, PATH: bin + path.delimiter + f.env.PATH };
  for (const args of [["status", "hermes", "--check"], ["doctor", "hermes"]]) {
    const result = await f.run([...args, "--json"], vars, project);
    const status = JSON.parse(result.stdout);
    assert.equal(status.projectName, "project-only", result.stdout);
    assert.equal(status.privacy, "structure-only");
    assert.equal(status.destination, endpoint);
  }
  const overridden = await f.run(["status", "hermes", "--project-dir", f.home, "--json"], vars, project);
  assert.equal(JSON.parse(overridden.stdout).projectName, "global");
  assert.equal(JSON.parse(overridden.stdout).privacy, "content-on-by-default");
  assert.equal(checks, 2);
  // The E2B Python job supplies this interpreter to compare the two real
  // resolvers, not a reimplementation of the Python contract in JavaScript.
  if (process.env.HERMES_RESOLVER_PYTHON) {
    const { spawnSync } = require("node:child_process");
    const runtime = spawnSync(process.env.HERMES_RESOLVER_PYTHON, ["-I", "-c",
      "import json,os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from traceai_hermes_agent._config import resolve; c=resolve(os.environ.get,project=Path.cwd()); print(json.dumps({k:c[k] for k in ('enabled','project_name','endpoint','capture_content')}))",
      path.resolve(__dirname, "../../../../python/frameworks/hermes-agent")], {
      cwd: project, env: { ...f.env, ...vars }, encoding: "utf8",
    });
    assert.equal(runtime.status, 0, runtime.stderr);
    assert.deepEqual(JSON.parse(runtime.stdout), { enabled: true, project_name: "project-only", endpoint, capture_content: false });
  }
});

test("Hermes project-only setup disables and re-enables from cwd, preserving explicit directory precedence", async (t) => {
  const f = await fixture(t);
  const project = path.join(f.home, 'project');
  const host = path.join(f.home, 'profile');
  const bin = path.join(f.home, 'bin');
  await fs.mkdir(project); await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, 'hermes'), `#!${process.execPath}\nconsole.log(process.argv.includes('--version')?'new-auto-updated-host':JSON.stringify([{name:'futureagi',version:'0.1.1',status:'enabled'}]));\n`, { mode: 0o700 });
  const vars = { HERMES_HOME: host, PATH: bin + path.delimiter + f.env.PATH };
  const server = http.createServer((q, r) => { q.resume(); q.on('end', () => { r.setHeader('Content-Type', 'application/json'); r.end('{}'); }); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise(r => server.close(r)));
  const configured = await f.run(['setup', 'hermes', '--scope', 'project', '--project-dir', project, '--configure-only', '--json'], {
    ...vars, FI_API_KEY: 'synthetic-project-key', FI_SECRET_KEY: 'synthetic-project-secret',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  });
  assert.equal(configured.code, 0, configured.stdout);
  await assert.rejects(fs.stat(path.join(host, 'futureagi.json')), { code: 'ENOENT' });
  assert.equal((await f.run(['disable', 'hermes', '--json'], vars, project)).code, 0);
  const explicit = await f.run(['enable', 'hermes', '--project-dir', f.home, '--json'], vars, project);
  assert.equal(explicit.code, 2);
  assert.equal(JSON.parse(explicit.stdout).code, 'SETUP_REQUIRED');
  const enabled = await f.run(['enable', 'hermes', '--json'], vars, project);
  assert.equal(enabled.code, 0, enabled.stdout);
  assert.equal(JSON.parse(enabled.stdout).enabled, true);
  const manifest = JSON.parse(await fs.readFile(path.join(f.env.FI_AGENT_CONFIG_DIR, 'installation.json')));
  assert.equal(manifest.disabled.hermes, false);
  assert.equal(manifest.generations.hermes, 2); // setup + disable advance the fence; enable never unfences old batches
});

test("Hermes uninstall uses core ownership cleanup and preserves unrelated files", async (t) => {
  const f = await fixture(t);
  const host = path.join(f.home, 'hermes-profile'), bin = path.join(f.home, 'bin');
  await fs.mkdir(bin); await fs.mkdir(host);
  await fs.writeFile(path.join(host, 'unrelated.json'), '{}');
  await fs.writeFile(path.join(bin, 'hermes'), `#!${process.execPath}\nconsole.log('[]');\n`, { mode: 0o700 });
  const vars = { HERMES_HOME: host, PATH: bin + path.delimiter + f.env.PATH };
  const server = http.createServer((q, r) => { q.resume(); q.on('end', () => { r.setHeader('Content-Type', 'application/json'); r.end('{}'); }); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise(r => server.close(r)));
  assert.equal((await f.run(['setup', 'hermes', '--configure-only', '--json'], {
    ...vars, FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  })).code, 0);
  assert.equal((await f.run(['uninstall', 'hermes', '--json'], vars)).code, 0);
  await assert.rejects(fs.stat(path.join(host, 'futureagi.json')), { code: 'ENOENT' });
  assert.equal(await fs.readFile(path.join(host, 'unrelated.json'), 'utf8'), '{}');
});

test("Hermes diagnostics never send environment keys to an unapproved repository collector", async (t) => {
  const f = await fixture(t);
  let calls = 0;
  const server = http.createServer((q, r) => { calls++; q.resume(); r.end("{}"); });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise(r => server.close(r)));
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  await fs.mkdir(path.join(f.home, ".futureagi"));
  await fs.writeFile(path.join(f.home, ".futureagi/agents.json"), JSON.stringify({ hermes: {
    schemaVersion: 1, profile: "global", enabled: true, captureContent: true, transport: { endpoint },
  } }), { mode: 0o600 });
  await fs.mkdir(f.env.FI_AGENT_CONFIG_DIR, { recursive: true });
  await fs.writeFile(path.join(f.env.FI_AGENT_CONFIG_DIR, "credentials.json"), JSON.stringify({
    schemaVersion: 1, defaultProfile: "global", profiles: { global: {
      endpoint, projectName: "hermes-agent", apiKey: "saved-api", secretKey: "saved-secret", captureContent: true,
    } },
  }), { mode: 0o600 });
  const vars = { HERMES_HOME: path.join(f.home, "unconfigured-home"), FI_API_KEY: "env-api", FI_SECRET_KEY: "env-secret" };
  const blocked = JSON.parse((await f.run(["doctor", "hermes", "--json"], vars)).stdout);
  assert.equal(blocked.enabled, false);
  assert.equal(blocked.privacy, "content-on-by-default");
  assert.equal(calls, 0);
  // An endpoint override does not independently approve a repository-selected
  // profile. Remove that untrusted profile selection before approving the URL.
  const profileBlocked = JSON.parse((await f.run(["doctor", "hermes", "--json"], { ...vars, FI_OTLP_ENDPOINT: endpoint })).stdout);
  assert.equal(profileBlocked.enabled, false); assert.equal(calls, 0);
  await fs.writeFile(path.join(f.home, ".futureagi/agents.json"), JSON.stringify({ hermes: {
    schemaVersion: 1, enabled: true, transport: { endpoint },
  } }), { mode: 0o600 });
  const approved = JSON.parse((await f.run(["doctor", "hermes", "--json"], { ...vars, FI_OTLP_ENDPOINT: endpoint })).stdout);
  assert.equal(approved.enabled, true);
  assert.equal(approved.privacy, "content-on-by-default");
  assert.equal(calls, 1);
  const noKeys = JSON.parse((await f.run(["doctor", "hermes", "--json"], { HERMES_HOME: vars.HERMES_HOME })).stdout);
  assert.equal(noKeys.enabled, false);
  assert.equal(noKeys.credentials, "missing");
  assert.equal(noKeys.privacy, "content-on-by-default");
  assert.equal(calls, 1);
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
