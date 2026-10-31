const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const {
  FileSecretStore,
  parseAuth,
  newCredentials,
} = require("../dist/src/security/credentials.js");
const { readProtected } = require("../dist/src/security/files.js");
const { agentEnvironment } = require("../dist/src/cli/run.js");
const { ImportPlanner } = require("../dist/src/import.js");
const reference = require("../contract/reference.cjs");
const base = path.resolve(__dirname, "../../../../../cli-tests");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
function fixture(t) {
  const root = fs.mkdtempSync(path.join(base, "fixture-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}
function cli(root, args, input) {
  return spawnSync(
    process.execPath,
    [path.resolve(__dirname, "../bin/fi-agent.cjs"), ...args],
    {
      input,
      encoding: "utf8",
      env: {
        PATH: process.env.PATH,
        HOME: root,
        FI_AGENT_CONFIG_HOME: path.join(root, "config"),
        FI_AGENT_STATE_HOME: path.join(root, "state"),
      },
      timeout: 10000,
    },
  );
}
test("auth uses explicit protected stdin and separate file store; never config or output secrets", (t) => {
  const root = fixture(t);
  const input = JSON.stringify({
    api_key: "synthetic-api-canary",
    secret_key: "synthetic-secret-canary",
  });
  const result = cli(
    root,
    [
      "auth",
      "add",
      "--profile",
      "work",
      "--project",
      "fixture",
      "--stdin",
      "--storage",
      "file",
    ],
    input,
  );
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.ok(!result.stdout.includes("canary"));
  assert.ok(!result.stderr.includes("canary"));
  const config = fs.readFileSync(path.join(root, "config/config.json"), "utf8");
  assert.ok(!config.includes("canary"));
  const c = JSON.parse(config),
    ref = c.profiles.work.credentialRef;
  const secret = JSON.parse(
    fs.readFileSync(
      path.join(root, "config/credentials", ref + ".json"),
      "utf8",
    ),
  );
  assert.equal(secret.api_key, "synthetic-api-canary");
  assert.equal(
    fs.statSync(path.join(root, "config/credentials", ref + ".json")).mode &
      0o777,
    0o600,
  );
  const rotate = cli(
    root,
    [
      "auth",
      "add",
      "--profile",
      "work",
      "--project",
      "fixture",
      "--stdin",
      "--storage",
      "file",
    ],
    JSON.stringify({ api_key: "rotated", secret_key: "rotated" }),
  );
  assert.equal(rotate.status, 0);
  assert.equal(
    JSON.parse(
      fs.readFileSync(
        path.join(root, "config/credentials", ref + ".json"),
        "utf8",
      ),
    ).identity_key,
    secret.identity_key,
  );
});
test("auth rejects argv secrets, ambiguous input, unknown modes and unprotected files", (t) => {
  const root = fixture(t);
  for (const args of [["--api-key", "bad"], ["--stdin", "--tty"], ["--tty"]]) {
    const r = cli(
      root,
      ["auth", "add", "--profile", "work", "--project", "fixture", ...args],
      "{}",
    );
    assert.equal(r.status, 2, r.stdout);
  }
  const f = path.join(root, "secret.json");
  fs.writeFileSync(f, "{}", { mode: 0o644 });
  assert.throws(() => readProtected(f), /UNSAFE_FILE/);
  fs.symlinkSync(f, path.join(root, "linked"));
  assert.throws(() => readProtected(path.join(root, "linked")));
  assert.throws(() => parseAuth(Buffer.alloc(16385)));
  assert.throws(() =>
    parseAuth(Buffer.from('{"api_key":"x\\n","secret_key":"y"}')),
  );
});
test("shipped executable has no adapter enabled, never silently installs a logo", (t) => {
  const root = fixture(t);
  const r = cli(root, [
    "enable",
    "codex",
    "--surface",
    "cli",
    "--profile",
    "work",
    "--project",
    "fixture",
  ]);
  assert.equal(r.status, process.platform === "darwin" ? 3 : 5);
  assert.match(
    r.stdout,
    process.platform === "darwin" ? /HELD_AUTH/ : /UNSUPPORTED_AGENT_SURFACE/,
  );
  assert.equal(fs.existsSync(path.join(root, "config")), false);
  const status = cli(root, ["status"]);
  assert.equal(status.status, 7);
  assert.match(status.stdout, /RUNTIME_UNAVAILABLE/);
});
test("run environment strips FI credentials and refuses provider/permission overrides", () => {
  const env = agentEnvironment(
    {
      FI_API_KEY: "canary",
      FI_SECRET_KEY: "canary",
      PATH: "/bin",
      OPENAI_API_KEY: "user-provider-key",
    },
    {},
  );
  assert.equal(env.FI_API_KEY, undefined);
  assert.equal(env.FI_SECRET_KEY, undefined);
  assert.equal(env.OPENAI_API_KEY, "user-provider-key");
  for (const key of [
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "PERMISSIONS",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
  ])
    assert.throws(() => agentEnvironment({}, { [key]: "bad" }));
});
test("import dry-run is sanitized, bounded, stable, and commit refuses changed source", async (t) => {
  const root = fixture(t),
    source = path.join(root, "fixture.json");
  const raw = [
    {
      schema: "fixture-v1",
      session: "s",
      turn: "t",
      id: "r",
      unit: "request",
      prompt: "CANARY_PRIVATE_CONTENT",
    },
  ];
  fs.writeFileSync(source, JSON.stringify(raw), { mode: 0o600 });
  const planner = new ImportPlanner(path.join(root, "plans")),
    context = {
      identityKey: Buffer.alloc(32, 1),
      policyRevision: "1",
      sourceSchema: "fixture-v1",
    };
  const plan = planner.plan(reference, source, context);
  assert.ok(!JSON.stringify(plan).includes("CANARY_PRIVATE_CONTENT"));
  assert.ok(
    !fs
      .readFileSync(path.join(root, "plans/plan.json"), "utf8")
      .includes("CANARY_PRIVATE_CONTENT"),
  );
  const facts = [];
  assert.deepEqual(
    await planner.commit(plan.id, reference, source, context, async (f) =>
      facts.push(f),
    ),
    { admitted: 1, pending: 0 },
  );
  assert.equal(facts[0].content, undefined);
  raw[0].id = "changed";
  fs.writeFileSync(source, JSON.stringify(raw), { mode: 0o600 });
  await assert.rejects(
    planner.commit(plan.id, reference, source, context, async () => {}),
    /SOURCE_CHANGED/,
  );
});

test("import keeps conflicts pending without censoring independent source work", async (t) => {
  const { AgentError } = require("../dist/src/security/files.js");
  const root = fixture(t),
    source = path.join(root, "fixture.json");
  fs.writeFileSync(
    source,
    JSON.stringify(
      ["old", "later"].map((id) => ({
        schema: "fixture-v1",
        session: "s",
        turn: "t",
        id,
        unit: "request",
      })),
    ),
    { mode: 0o600 },
  );
  const planner = new ImportPlanner(path.join(root, "plans"));
  const context = {
    identityKey: Buffer.alloc(32, 1),
    policyRevision: "1",
    sourceSchema: "fixture-v1",
  };
  const plan = planner.plan(reference, source, context);
  for (const code of ["HELD_IMMUTABLE_CONFLICT", "PAUSED_STORAGE"]) {
    let calls = 0;
    const result = await planner.commit(
      plan.id,
      reference,
      source,
      context,
      async () => {
        if (++calls === 1) throw new AgentError(code);
      },
    );
    assert.deepEqual(
      result,
      code === "HELD_IMMUTABLE_CONFLICT"
        ? { admitted: 1, pending: 1 }
        : { admitted: 0, pending: 2 },
    );
    assert.equal(calls, code === "HELD_IMMUTABLE_CONFLICT" ? 2 : 1);
  }
});
