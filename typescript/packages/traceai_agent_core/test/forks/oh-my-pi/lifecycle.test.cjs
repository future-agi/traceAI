const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const c = require("../../../dist/src/index.js");
async function fixture(t, handler) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-agent-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    const data = [];
    req.on("data", (b) => data.push(b));
    req.on("end", () => {
      requests.push({
        headers: req.headers,
        body: JSON.parse(Buffer.concat(data).toString()),
        url: req.url,
      });
      if (handler) handler(req, res, requests.at(-1));
      else {
        res.setHeader("Content-Type", "application/json");
        res.end("{}");
      }
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const options = { agent: "pi", home, env: {} };
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  const setup = (extra) =>
    c.setup({
      ...options,
      credentials: { apiKey: "synthetic-api", secretKey: "synthetic-secret" },
      projectName: "unit-project",
      otlpEndpoint: endpoint,
      configureOnly: true,
      ...extra,
    });
  return { home, options, requests, endpoint, setup };
}
test("validation uses empty OTLP and three headers; writes private files only after acceptance", async (t) => {
  const f = await fixture(t),
    result = await f.setup();
  assert.equal(result.code, "CONFIGURED_NOT_REGISTERED");
  assert.equal(result.registered, false);
  assert.deepEqual(f.requests[0].body, { resourceSpans: [] });
  assert.equal(f.requests[0].headers["x-api-key"], "synthetic-api");
  assert.equal(f.requests[0].headers["x-secret-key"], "synthetic-secret");
  assert.equal(f.requests[0].headers["x-project-name"], "unit-project");
  const paths = await c.configPaths(f.options);
  for (const p of [paths.credentials, paths.manifest, paths.user])
    assert.equal((await fs.stat(p)).mode & 0o777, 0o600);
  assert.equal((await fs.stat(paths.configDir)).mode & 0o777, 0o700);
  assert.equal(
    (await fs.stat(path.dirname(paths.configDir))).mode & 0o777,
    0o700,
  );
  const config = await c.resolveConfig(f.options);
  assert.equal(config.enabled, true);
  assert.equal(config.captureContent, true);
  assert.ok(!JSON.stringify(config).includes("synthetic-secret"));
});
for (const [status, body, type, expected] of [
  [401, "{}", "application/json", "AUTH_REJECTED"],
  [403, "{}", "application/json", "AUTH_REJECTED"],
  [429, "{}", "application/json", "RATE_LIMITED"],
  [503, "{}", "application/json", "UNVERIFIED"],
  [200, "<html>ok</html>", "text/html", "UNVERIFIED"],
  [200, '{"success":true}', "application/json", "UNVERIFIED"],
  [
    200,
    '{"partialSuccess":{"rejectedSpans":"1"}}',
    "application/json",
    "PARTIAL_REJECTION",
  ],
]) {
  test(`failed validation ${status}/${expected} persists nothing`, async (t) => {
    const f = await fixture(t, (_q, r) => {
      r.statusCode = status;
      r.setHeader("Content-Type", type);
      r.end(body);
    });
    const result = await f.setup();
    assert.equal(result.code, expected);
    const p = await c.configPaths(f.options);
    for (const file of [p.credentials, p.manifest, p.user])
      await assert.rejects(fs.stat(file), { code: "ENOENT" });
  });
}
test("redirects are never followed and slow responses hit the validation deadline", async (t) => {
  const f = await fixture(t, (_q, r) => {
    r.statusCode = 302;
    r.setHeader("Location", "http://127.0.0.1:1/stolen");
    r.end();
  });
  assert.equal((await f.setup()).code, "UNVERIFIED");
  assert.equal(f.requests.length, 1);
  const slow = await fixture(t, () => {});
  const started = Date.now();
  const result = await c.validateCredentials({
    endpoint: slow.endpoint,
    projectName: "test",
    credentials: { apiKey: "a", secretKey: "b" },
    timeoutMs: 30,
  });
  assert.equal(result.code, "UNVERIFIED");
  assert.ok(Date.now() - started < 1000);
});
test("resolve precedence, complete env pair and unconditional kill switch", async (t) => {
  const f = await fixture(t);
  await f.setup();
  const config = await c.resolveConfig({
    ...f.options,
    env: {
      FI_PROJECT_NAME: "environment-project",
      FI_API_KEY: "env-api",
      FI_SECRET_KEY: "env-secret",
      FI_AGENT_CAPTURE_CONTENT: "false",
    },
    flags: { projectName: "flag-project" },
  });
  assert.equal(config.projectName, "flag-project");
  assert.equal(config.provenance.projectName, "flags");
  assert.equal(config.captureContent, false);
  assert.equal(config.credentials.apiKey, "env-api");
  await assert.rejects(
    c.resolveConfig({ ...f.options, env: { FI_API_KEY: "one-only" } }),
    /INCOMPLETE_CREDENTIAL_OVERRIDE/,
  );
  const disabled = await c.resolveConfig({
    ...f.options,
    env: { FI_AGENT_ENABLED: "false" },
    flags: { enabled: true },
  });
  assert.equal(disabled.enabled, false);
});
test("project cannot exfiltrate saved keys or raise privacy; opt-out persists", async (t) => {
  const f = await fixture(t);
  await f.setup({ captureContent: false });
  const projectDir = path.join(f.home, "project");
  await fs.mkdir(projectDir);
  const p = await c.configPaths({ ...f.options, projectDir });
  await c.writeProtected(
    p.project,
    JSON.stringify({
      schemaVersion: 1,
      captureContent: true,
      transport: { endpoint: "https://attacker.invalid/v1/traces" },
    }),
  );
  const config = await c.resolveConfig({ ...f.options, projectDir });
  assert.equal(config.captureContent, false);
  assert.equal(config.enabled, false);
  assert.ok(config.issues.includes("DESTINATION_NOT_APPROVED"));
  const before = f.requests.length;
  const tracer = c.createTurnTracer({ config });
  tracer
    .startTurn({ sessionId: "s", turnId: "t", input: "do not export" })
    .end();
  await tracer.shutdown();
  assert.equal(f.requests.length, before);
  await assert.rejects(
    c.setup({
      ...f.options,
      otlpEndpoint: "https://attacker.invalid/v1/traces",
      configureOnly: true,
    }),
    /DESTINATION_APPROVAL_REQUIRED/,
  );
});
test("FI_OTLP_ENDPOINT is exact; FI_BASE_URL must be an origin; stricter error/input privacy wins", async (t) => {
  const f = await fixture(t);
  await f.setup();
  const config = await c.resolveConfig({
    ...f.options,
    env: {
      FI_OTLP_ENDPOINT: f.endpoint + "/exact",
      FI_BASE_URL: "https://ignored.invalid",
      FI_HIDE_ERROR_DETAILS: "true",
      FI_HIDE_INPUTS: "true",
    },
  });
  assert.equal(config.endpoint, f.endpoint + "/exact");
  assert.equal(config.hideErrorDetails, true);
  assert.equal(config.hideInputs, true);
  assert.throws(
    () => c.endpointFromBase("https://example.invalid/prefix"),
    /BASE_URL/,
  );
  for (const url of [
    "http://example.invalid/traces",
    "https://user:secret@example.invalid/traces",
    "https://example.invalid/traces?token=x",
  ])
    assert.throws(() => c.endpoint(url));
});
test("host overrides are honored; unknown host directories require discovery", async (t) => {
  const f = await fixture(t);
  const p = await c.configPaths({
    ...f.options,
    agent: "claude-code",
    env: {
      CLAUDE_CONFIG_DIR: path.join(f.home, "custom-claude"),
      FI_AGENT_CONFIG_DIR: path.join(f.home, "custom-fi"),
    },
  });
  assert.equal(p.user, path.join(f.home, "custom-claude", "futureagi.json"));
  assert.equal(p.configDir, path.join(f.home, "custom-fi"));
  await assert.rejects(
    c.configPaths({ ...f.options, agent: "unknown-host" }),
    /DISCOVERY_REQUIRED/,
  );
});
test("safe writes reject symlinks, broad modes and stale merges", async (t) => {
  const f = await fixture(t),
    victim = path.join(f.home, "victim");
  await c.writeProtected(victim, "{}");
  const symlink = path.join(f.home, "link");
  await fs.symlink(victim, symlink);
  await assert.rejects(c.writeProtected(symlink, '{"changed":true}'));
  assert.equal(await fs.readFile(victim, "utf8"), "{}");
  await assert.rejects(
    c.writeProtected(victim, "new", c.contentHash("old")),
    /CONCURRENT_SAVE/,
  );
  await fs.chmod(victim, 0o644);
  await assert.rejects(c.readProtected(victim), /UNSAFE_FILE/);
});
const { fork } = require("node:child_process");
const { once } = require("node:events");
function lockWorker(t, ...args) {
  const worker = fork(path.join(__dirname, "lock-worker.cjs"), args, {
    stdio: ["ignore", "ignore", "pipe", "ipc"],
  });
  let stderr = "";
  worker.stderr.on("data", (chunk) => (stderr += chunk));
  const exited = once(worker, "exit");
  t.after(() => {
    if (worker.exitCode === null && worker.signalCode === null)
      worker.kill("SIGKILL");
  });
  return {
    worker,
    exited,
    async success() {
      const [code, signal] = await exited;
      assert.equal(code, 0, `${signal ?? ""} ${stderr}`);
    },
  };
}
test(
  "killed manifest writer is recovered and persistent disable fences an already active tracer",
  { timeout: 25000 },
  async (t) => {
    const f = await fixture(t);
    await f.setup();
    const config = await c.resolveConfig(f.options),
      tracer = c.createTurnTracer({ config });
    const turn = tracer.startTurn({
      sessionId: "s",
      turnId: "in-flight",
      input: "NEVER_EXPORT_AFTER_DISABLE",
    });
    const holder = lockWorker(t, "hold", config.paths.manifest);
    const [message] = await once(holder.worker, "message");
    assert.equal(message, "locked");
    assert((await fs.lstat(config.paths.manifest + ".write-lock")).isDirectory());
    holder.worker.kill("SIGKILL");
    await holder.exited;
    const before = f.requests.length,
      start = Date.now();
    await c.disable(f.options);
    assert(Date.now() - start < 15000, "dead lease recovery must be bounded");
    assert.equal(c.collectionEnabled(config), false);
    turn.end({ output: "NEVER_EXPORT_AFTER_DISABLE" });
    await tracer.shutdown();
    assert.equal(f.requests.length, before);
    await assert.rejects(fs.lstat(config.paths.manifest + ".write-lock"), {
      code: "ENOENT",
    });
    assert.equal((await fs.stat(config.paths.manifest)).mode & 0o777, 0o600);
  },
);
test("aged empty legacy lock no longer permanently prevents disable", async (t) => {
  const f = await fixture(t);
  await f.setup();
  const config = await c.resolveConfig(f.options),
    lock = config.paths.manifest + ".lock";
  await fs.writeFile(lock, "", { mode: 0o600 });
  const aged = new Date(Date.now() - 120000);
  await fs.utimes(lock, aged, aged);
  await c.disable(f.options);
  assert.equal(c.collectionEnabled(config), false);
  assert((await fs.lstat(lock)).isFile(), "legacy ownerless claims stay inert");
});
test(
  "live configuration lock waits; concurrent processes merge fields without lost writes",
  { timeout: 20000 },
  async (t) => {
    const f = await fixture(t),
      file = path.join(f.home, "shared.json");
    await c.writeProtected(file, "{}");
    const holder = lockWorker(t, "hold", file);
    await once(holder.worker, "message");
    assert.equal((await fs.lstat(file + ".write-lock")).mode & 0o777, 0o700);
    const writers = ["one", "two", "three", "four", "five", "six"].map((id) =>
      lockWorker(t, "write", file, id),
    );
    await new Promise((r) => setTimeout(r, 300));
    assert(
      writers.every((w) => w.worker.exitCode === null),
      "live holder must not be displaced",
    );
    holder.worker.send("release");
    await holder.success();
    await Promise.all(writers.map((w) => w.success()));
    const result = JSON.parse(await fs.readFile(file, "utf8"));
    assert.deepEqual(result, {
      held: true,
      one: 8,
      two: 8,
      three: 8,
      four: 8,
      five: 8,
      six: 8,
    });
    // Real manifest writers preserve each other's disable generation and fence snapshots.
    await f.setup();
    const config = await c.resolveConfig(f.options);
    await Promise.all(
      ["pi", "oh-my-pi", "codex"].map((agent) =>
        lockWorker(t, "disable", f.home, agent).success(),
      ),
    );
    assert.equal(c.collectionEnabled(config), false);
    const manifest = JSON.parse(
      await fs.readFile(config.paths.manifest, "utf8"),
    );
    for (const agent of ["pi", "oh-my-pi", "codex"]) {
      assert.equal(manifest.disabled[agent], true);
      assert.equal(manifest.generations[agent], (agent === f.options.agent ? config.generation : 0) + 1);
    }
  },
);
test(
  "lock wait is bounded and cannot steal a live writer's refreshed lease",
  { timeout: 25000 },
  async (t) => {
    const f = await fixture(t),
      file = path.join(f.home, "live.json");
    await c.writeProtected(file, "{}");
    const holder = lockWorker(t, "hold", file);
    await once(holder.worker, "message");
    const started = Date.now();
    await assert.rejects(
      c.writeProtected(file, '{"stolen":true}'),
      /CONCURRENT_SAVE/,
    );
    assert(
      Date.now() - started >= 10000,
      "must exercise the heartbeat past its stale interval",
    );
    assert(
      Date.now() - started < 18000,
      "bounded retries must not block the host forever",
    );
    assert.equal(await fs.readFile(file, "utf8"), "{}");
    holder.worker.send("release");
    await holder.success();
    assert.equal(JSON.parse(await fs.readFile(file, "utf8")).held, true);
    await c.updateJson(file, (old) => ({ ...old, next: true }));
  },
);
test("lock recovery refuses symlinks, hardlinks, foreign ownership and broad permissions", async (t) => {
  const f = await fixture(t),
    file = path.join(f.home, "target.json"),
    lock = file + ".write-lock",
    victim = path.join(f.home, "victim");
  await fs.mkdir(victim, { mode: 0o700 });
  await fs.symlink(victim, lock);
  await assert.rejects(c.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  await fs.unlink(lock);
  await fs.mkdir(lock, { mode: 0o755 });
  await assert.rejects(c.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  await fs.rmdir(lock);
  await fs.writeFile(lock, "", { mode: 0o600 });
  await fs.link(lock, path.join(f.home, "hardlink"));
  await assert.rejects(c.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  await fs.unlink(lock);
  if (process.getuid?.() === 0) {
    await fs.mkdir(lock, { mode: 0o700 });
    await fs.chown(lock, 65534, 65534);
    await assert.rejects(c.writeProtected(file, "{}"), /UNSAFE_LOCK/);
  }
  assert((await fs.stat(victim)).isDirectory());
  await assert.rejects(fs.stat(file), { code: "ENOENT" });
});
test("failed installation reports NOT REGISTERED, retry is idempotent, status never infers connection", async (t) => {
  const f = await fixture(t);
  let installs = 0,
    registered = false;
  const adapter = {
    id: "pi",
    version: "0.1.0",
    detect: async () => ({ supported: true, version: "test" }),
    install: async () => {
      installs++;
      registered = true;
      return { registered };
    },
    inspect: async () => ({ registered, trust: "required" }),
    uninstall: async () => ({ registered: false }),
  };
  const failed = await f.setup({
    configureOnly: false,
    adapter: {
      ...adapter,
      install: async () => {
        throw new Error("raw sensitive provider error");
      },
    },
  });
  assert.equal(failed.code, "INSTALLATION_FAILED");
  assert.equal(failed.configured, true);
  const status = await c.inspectStatus(f.options);
  assert.equal(status.registration, "NOT_REGISTERED");
  assert.equal(status.activation, "NOT_OBSERVED");
  assert.equal(status.productReadback, "unverified");
  assert.equal(status.exitCode, 2);
  assert.ok(!JSON.stringify(status).includes("synthetic-secret"));
  for (let n = 0; n < 2; n++)
    assert.equal(
      (await f.setup({ configureOnly: false, adapter })).registered,
      true,
    );
  assert.equal(installs, 2);
  assert.equal(
    (await c.inspectStatus({ ...f.options, adapter })).trust,
    "required",
  );
});
test("disable needs no credentials and fences stale tracer queues before export", async (t) => {
  const f = await fixture(t);
  await f.setup();
  const config = await c.resolveConfig(f.options),
    tracer = c.createTurnTracer({ config });
  tracer
    .startTurn({ sessionId: "s", turnId: "1", input: "queued content" })
    .end();
  const n = f.requests.length;
  await fs.unlink(config.paths.credentials);
  await c.disable(f.options);
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(f.requests.length, n);
  assert.ok(tracer.state.dropped >= 1);
  assert.equal(c.collectionEnabled(config), false);
});
test("disable generation prevents stale queued spans from reviving after enable", async (t) => {
  const f = await fixture(t);
  await f.setup();
  const config = await c.resolveConfig(f.options),
    tracer = c.createTurnTracer({ config });
  tracer.startTurn({ sessionId: "s", turnId: "stale" }).end();
  await c.disable(f.options);
  await c.enable(f.options);
  const n = f.requests.length;
  await tracer.shutdown();
  assert.equal(f.requests.length, n);
  assert.equal(c.collectionEnabled(config), false);
  assert.equal(c.collectionEnabled(await c.resolveConfig(f.options)), true);
});
test("repository cannot select a different global credential profile", async (t) => {
  const f = await fixture(t);
  await f.setup();
  const projectDir = path.join(f.home, "repository");
  await fs.mkdir(projectDir);
  const p = await c.configPaths({ ...f.options, projectDir });
  await c.updateJson(p.credentials, (store) => ({
    ...store,
    profiles: {
      ...store.profiles,
      other: {
        ...Object.values(store.profiles)[0],
        endpoint: "https://other.invalid/traces",
        projectName: "other-project",
      },
    },
  }));
  await c.writeProtected(
    p.project,
    JSON.stringify({
      schemaVersion: 1,
      profile: "other",
      projectName: "other-project",
      transport: { endpoint: "https://other.invalid/traces" },
    }),
  );
  const config = await c.resolveConfig({ ...f.options, projectDir });
  assert.equal(config.destinationApproved, false);
  assert.equal(config.enabled, false);
});
test("explicit privacy override beats user file while a repository cannot raise its ceiling", async (t) => {
  const f = await fixture(t);
  await f.setup({ captureContent: false });
  const raised = await c.resolveConfig({
    ...f.options,
    env: { FI_AGENT_CAPTURE_CONTENT: "true" },
  });
  assert.equal(raised.captureContent, true);
  const lowered = await c.resolveConfig({
    ...f.options,
    env: { FI_AGENT_CAPTURE_CONTENT: "true", FI_HIDE_INPUTS: "true" },
  });
  assert.equal(lowered.hideInputs, true);
});
test("disable works even when a host configuration directory has not been discovered", async (t) => {
  const f = await fixture(t);
  await c.disable({ ...f.options, agent: "hermes" });
  const file = c.controlPaths(f.options).manifest;
  assert.equal(JSON.parse(await fs.readFile(file)).disabled.hermes, true);
});
test("message/image privacy and inherited error-detail privacy are never weakened", async (t) => {
  const f = await fixture(t);
  await f.setup();
  for (const key of [
    "FI_HIDE_INPUTS",
    "FI_HIDE_INPUT_TEXT",
    "FI_HIDE_INPUT_MESSAGES",
    "FI_HIDE_INPUT_IMAGES",
  ]) {
    const config = await c.resolveConfig({
      ...f.options,
      env: { [key]: "true" },
    });
    assert.equal(config.hideInputs, true);
    assert.equal(config.hideErrorDetails, true);
  }
  const output = await c.resolveConfig({
    ...f.options,
    env: { FI_HIDE_OUTPUT_MESSAGES: "true" },
  });
  assert.equal(output.hideOutputs, true);
  assert.equal(output.hideErrorDetails, true);
});
test("environment keys do not authorize repository destinations; explicit destinations and bound profiles do", async (t) => {
  const f = await fixture(t);
  const projectDir = path.join(f.home, "project");
  await fs.mkdir(projectDir);
  const opts = {
    ...f.options,
    projectDir,
    env: {
      FI_API_KEY: "synthetic-env-api",
      FI_SECRET_KEY: "synthetic-env-secret",
      FI_AGENT_ENABLED: "true",
    },
  };
  const p = await c.configPaths(opts);
  const repo = {
    schemaVersion: 1,
    projectName: "repo",
    transport: { endpoint: f.endpoint },
  };
  await c.writeProtected(p.project, JSON.stringify(repo));
  assert.equal((await c.resolveConfig(opts)).destinationApproved, false);
  assert.equal(
    (
      await c.resolveConfig({
        ...opts,
        env: { ...opts.env, FI_OTLP_ENDPOINT: f.endpoint },
      })
    ).destinationApproved,
    false,
    "endpoint alone cannot approve repository project",
  );
  const explicit = {
    ...opts,
    env: { ...opts.env, FI_OTLP_ENDPOINT: f.endpoint, FI_PROJECT_NAME: "repo" },
  };
  assert.equal((await c.resolveConfig(explicit)).destinationApproved, true);
  assert.equal(
    (
      await c.resolveConfig({
        ...opts,
        flags: { transport: { endpoint: f.endpoint }, projectName: "repo" },
      })
    ).destinationApproved,
    true,
  );
  await c.setup({ ...explicit, scope: "project", configureOnly: true });
  assert.equal(
    (await c.resolveConfig(opts)).destinationApproved,
    true,
    "validated binding permits env credential rotation",
  );
  const other = path.join(f.home, "other");
  await fs.mkdir(other);
  const otherPaths = await c.configPaths({ ...opts, projectDir: other });
  await c.writeProtected(
    otherPaths.project,
    await fs.readFile(p.project, "utf8"),
  );
  assert.equal(
    (await c.resolveConfig({ ...opts, projectDir: other })).destinationApproved,
    false,
    "copied project profile is not approval",
  );
  await c.writeProtected(
    p.project,
    JSON.stringify({ ...repo, projectName: "changed-project" }),
  );
  assert.equal((await c.resolveConfig(opts)).destinationApproved, false);
});
test("quoted assignments redact complete escaped values and truncated captures without hiding public neighbors", () => {
  for (const key of [
    "OPENAI_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "clientSecret",
    "auth_token",
    "PASSWORD",
  ]) {
    for (const value of [
      '"SYNTH_SECRET"',
      "'SYNTH_SECRET'",
      String.raw`"SYNTH_SECRET\"TAIL"`,
      String.raw`'SYNTH_SECRET\'TAIL'`,
      '"SYNTH_SECRET\nTAIL"',
    ]) {
      const clean = c.redact(
        `${key}=${value} PUBLIC_SETTING=visible input_tokens=42`,
      );
      assert(!clean.includes("SYNTH_SECRET") && !clean.includes("TAIL"), clean);
      assert(
        clean.includes("PUBLIC_SETTING=visible") &&
          clean.includes("input_tokens=42"),
        clean,
      );
    }
    for (const quote of ['"', "'"]) {
      const clean = c.redact(`${key}=${quote}SYNTH_TRUNCATED`);
      assert(!clean.includes("SYNTH_TRUNCATED"));
    }
  }
});
test("text redaction covers quoted JSON credential keys and escaped values", () => {
  for (const key of [
    "password",
    "token",
    "access_token",
    "refreshToken",
    "apiKey",
    "secretKey",
    "client-secret",
    "authorization",
    "cookie",
    "private_key",
  ]) {
    const clean = c.redact(
      JSON.stringify({
        [key]: 'SYNTH_SECRET with \\" escapes',
        visible: "public",
      }),
    );
    assert(!clean.includes("SYNTH_SECRET"), key + clean);
    assert(clean.includes("public"));
    assert.equal(c.isSensitiveKey(key), true);
  }
  assert.equal(c.isSensitiveKey("token_count"), false);
  assert(!c.redact('{"password":"SYNTH_TRUNCATED').includes("SYNTH_TRUNCATED"));
});
