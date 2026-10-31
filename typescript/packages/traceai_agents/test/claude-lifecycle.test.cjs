const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const { spawn } = require("node:child_process");
const { frame } = require("@traceai/agent-core/client");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { paths, loadConfig } = require("../dist/src/config.js");
const { settings, adapter } = require("./fixtures/claude-cli.cjs");
const {
  normalizeClaudeSpan,
  CLAUDE_SCHEMA,
} = require("../dist/src/adapters/claude-code/native.js");
const source = require("../contract/claude-fixture.cjs");
const base = path.resolve(__dirname, "../../../../../");
const route = "claude-code.cli";
function fixture(t) {
  const root = fs.mkdtempSync(path.join(base, "rc-"));
  const binary = path.join(root, "claude");
  const env = {
    HOME: root,
    CLAUDE_CONFIG_DIR: path.join(root, "a"),
    PATH: root + ":" + path.dirname(process.execPath) + ":/usr/bin:/bin",
    FI_AGENT_CONFIG_HOME: path.join(root, "c"),
    FI_AGENT_STATE_HOME: path.join(root, "s"),
  };
  const p = paths(env);
  function version(value = "2.1.316") {
    fs.writeFileSync(
      binary,
      "#!" +
        process.execPath +
        "\n" +
        `if (process.argv[2] === '--version') console.log(${JSON.stringify(value + " (Claude Code)")});\n` +
        `else { require('node:fs').writeFileSync(${JSON.stringify(path.join(root, "ran"))}, JSON.stringify({args:process.argv.slice(2),telemetry:process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT ?? null})); process.exit(17); }\n`,
      { mode: 0o700 },
    );
  }
  version();
  function cli(args, input = "") {
    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [settings.entry, ...args], {
        env,
        cwd: root,
        stdio: ["pipe", "pipe", "pipe"],
      });
      let stdout = "",
        stderr = "";
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        reject(Error("CLI_TIMEOUT"));
      }, 15000);
      child.stdin.end(input);
      child.stdout.on("data", (c) => {
        stdout += c;
      });
      child.stderr.on("data", (c) => {
        stderr += c;
      });
      child.on("error", reject);
      child.on("close", (code) => {
        clearTimeout(timer);
        resolve({ code, stdout, stderr });
      });
    });
  }
  async function ok(args, input) {
    const result = await cli(args, input);
    assert.equal(result.code, 0, result.stdout + result.stderr);
    return JSON.parse(result.stdout);
  }
  const action = (command, ...args) => [
    command,
    "claude-code",
    "--surface",
    "cli",
    ...args,
  ];
  async function stop() {
    await cli(["runtime", "stop"]);
    const until = Date.now() + 3000;
    while (fs.existsSync(p.socket) && Date.now() < until)
      await new Promise((r) => setTimeout(r, 10));
    assert.equal(fs.existsSync(p.socket), false);
  }
  t.after(async () => {
    await stop();
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, binary, env, p, version, cli, ok, action, stop };
}
async function auth(f, value = "synthetic") {
  return f.ok(
    [
      "auth",
      "add",
      "--profile",
      "fixture",
      "--project",
      "fixture",
      "--storage",
      "file",
      "--stdin",
    ],
    JSON.stringify({ api_key: value, secret_key: value }),
  );
}
async function install(f) {
  await auth(f);
  await f.ok(f.action("enable", "--profile", "fixture"));
}
function secrets(f) {
  const ref = loadConfig(f.p).profiles.fixture.credentialRef;
  return JSON.parse(
    fs.readFileSync(path.join(f.p.credentials, ref + ".json"), "utf8"),
  );
}

test("completed native intervals without outcome evidence stay unknown, not success or partial", () => {
  for (const operation of [
    "claude_code.interaction",
    "claude_code.tool",
    "claude_code.llm_request",
    "claude_code.tool.execution",
    "claude_code.tool.blocked_on_user",
  ]) {
    const span = source.span(operation);
    span.attributes = span.attributes.filter(
      (a) => !["success", "decision"].includes(a.key),
    );
    const normalize = (s) =>
      normalizeClaudeSpan(s, source.resource, source.scope, {
        identityKey: Buffer.alloc(32, 1),
        sourceSchema: CLAUDE_SCHEMA,
        policyRevision: "1",
      });
    const fact = normalize(span);
    assert.equal(fact.outcome, "unknown");
    assert.equal(fact.incomplete, undefined);
    const wire = require("../dist/src/export/otlp.js").spanObject(fact);
    assert.equal(wire.status.code, 0);
    assert.equal(
      wire.attributes.find((a) => a.key === "fi.agent.outcome").value
        .stringValue,
      "unknown",
    );
    assert.equal(
      wire.attributes.find((a) => a.key === "fi.capture.partial"),
      undefined,
    );
    assert.equal(normalize({ ...span, status: { code: 2 } }).outcome, "error");
    for (const status of [1, "STATUS_CODE_OK"])
      assert.equal(
        normalize({ ...span, status: { code: status } }).outcome,
        "success",
      );
    for (const success of [true, false])
      assert.equal(
        normalize({
          ...span,
          attributes: [...span.attributes, ...source.kv({ success })],
        }).outcome,
        success ? "success" : "error",
      );
    assert.equal(
      normalize({
        ...span,
        attributes: [
          ...span.attributes,
          ...source.kv({ decision: "cancelled" }),
        ],
      }).outcome,
      "cancelled",
    );
    assert.equal(
      normalize({
        ...span,
        attributes: [...span.attributes, ...source.kv({ decision: "reject" })],
      }).outcome,
      "permission_denied",
    );
  }
});

test("pinned version preflight holds enable, repeated enable, doctor, update-check and run after auto-update", async (t) => {
  const f = fixture(t);
  await auth(f);
  f.version("2.1.999");
  for (const command of ["enable", "doctor"]) {
    const result = await f.cli(f.action(command, "--profile", "fixture"));
    assert.equal(result.code, 5, result.stdout);
    assert.match(result.stdout, /UNSUPPORTED_CLAUDE_VERSION/);
  }
  assert.deepEqual(loadConfig(f.p).routes, {});
  f.version();
  await f.ok(f.action("enable", "--profile", "fixture"));
  f.version("2.1.999");
  for (const args of [
    f.action("enable"),
    f.action("doctor"),
    f.action("update", "--check"),
    f.action("run", "--", "--print", "fixture"),
  ]) {
    const result = await f.cli(args);
    assert.equal(result.code, 5, result.stdout);
    assert.match(result.stdout, /UNSUPPORTED_CLAUDE_VERSION/);
  }
  assert.equal(fs.existsSync(path.join(f.root, "ran")), false);
  // Local cleanup is independent of the agent version, not another activation.
  await f.ok(f.action("uninstall"));
});

test("version probe is bounded and never echoes executable output", async (t) => {
  const f = fixture(t);
  await auth(f);
  for (const code of [
    "console.log('PRIVATE_CANARY'.repeat(200))",
    "console.error('PRIVATE_CANARY');process.exit(9)",
    "setInterval(()=>{},1000)",
  ]) {
    fs.writeFileSync(f.binary, "#!" + process.execPath + "\n" + code, {
      mode: 0o700,
    });
    const before = Date.now();
    const result = await f.cli(f.action("enable", "--profile", "fixture"));
    assert.equal(result.code, 5, result.stdout);
    assert.match(result.stdout, /CLAUDE_VERSION_UNAVAILABLE/);
    assert.ok(!result.stdout.includes("PRIVATE_CANARY"));
    assert.ok(!result.stderr.includes("PRIVATE_CANARY"));
    assert.ok(Date.now() - before < 6000);
  }
});

test("listener exhaustion launches the synthetic child untraced with its own exit status", async (t) => {
  const f = fixture(t);
  await install(f);
  for (let i = 0; i < 8; i++)
    await runtimeRequest(f.p.socket, { operation: "native-open", route });
  const result = await f.cli(f.action("run", "--", "--print", "fixture"));
  assert.equal(result.code, 17, result.stdout + result.stderr);
  assert.match(result.stderr, /launching without tracing/);
  const ran = JSON.parse(fs.readFileSync(path.join(f.root, "ran"), "utf8"));
  assert.deepEqual(ran.args, ["--print", "fixture"]);
  assert.equal(ran.telemetry, null);
  await assert.rejects(
    runtimeRequest(f.p.socket, { operation: "native-open", route }),
    /NATIVE_LISTENER_LIMIT/,
  );
});

test("native listener OS failures traverse the real runtime and launch untraced without leaking leases", async (t) => {
  const f = fixture(t);
  await install(f);
  await f.stop();
  const runtime = new Runtime(
    f.p,
    settings.storageEntry,
    settings.version,
    settings.registry,
  );
  await runtime.start();
  const { Server } = require("node:http");
  const listen = Server.prototype.listen;
  let injected = 0;
  let failure;
  Server.prototype.listen = function (...args) {
    assert.equal(args[0], 0);
    assert.equal(args[1], "127.0.0.1");
    injected++;
    const error = Object.assign(new Error("PRIVATE_LISTENER_CANARY"), {
      code: failure,
    });
    // Both OS event errors and synchronous listen errors must retain their classification.
    if (failure === "ENOMEM") throw error;
    setImmediate(() => this.emit("error", error));
    return this;
  };
  try {
    const failures = [
      "EMFILE",
      "ENFILE",
      "ENOMEM",
      "ENOBUFS",
      "EADDRINUSE",
      "EADDRNOTAVAIL",
      "EACCES",
      "EPERM",
      "EMFILE",
    ];
    for (failure of failures) {
      await assert.rejects(
        runtimeRequest(f.p.socket, { operation: "native-open", route }),
        /NATIVE_LISTENER_UNAVAILABLE/,
      );
      const result = await f.cli(f.action("run", "--", "--print", "fixture"));
      assert.equal(result.code, 17, result.stdout + result.stderr);
      assert.match(result.stderr, /launching without tracing/);
      assert.ok(
        !(result.stdout + result.stderr).includes("PRIVATE_LISTENER_CANARY"),
      );
      const ran = JSON.parse(fs.readFileSync(path.join(f.root, "ran"), "utf8"));
      assert.deepEqual(ran.args, ["--print", "fixture"]);
      assert.equal(ran.telemetry, null);
    }
    assert.equal(injected, failures.length * 2);
    failure = "UNCLASSIFIED_ERROR";
    fs.unlinkSync(path.join(f.root, "ran"));
    const unknown = await f.cli(f.action("run", "--", "--print", "fixture"));
    assert.equal(unknown.code, 7);
    assert.match(unknown.stdout, /LOCAL_UNAVAILABLE/);
    assert.equal(fs.existsSync(path.join(f.root, "ran")), false);
    assert.ok(
      !(unknown.stdout + unknown.stderr).includes("PRIVATE_LISTENER_CANARY"),
    );
    Server.prototype.listen = listen;
    const recovered = await runtimeRequest(f.p.socket, {
      operation: "native-open",
      route,
    });
    await runtimeRequest(f.p.socket, {
      operation: "native-close",
      route,
      lease: recovered.lease,
    });
  } finally {
    Server.prototype.listen = listen;
    await runtime.stop();
  }
});

test("native-open availability race falls back; explicit ownership and policy failures never do", async (t) => {
  const f = fixture(t);
  await auth(f);
  const previous = { ...process.env },
    cwd = process.cwd();
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, f.env);
  process.chdir(f.root);
  t.after(() => {
    process.chdir(cwd);
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, previous);
  });
  const context = {
    socketPath: path.join(f.root, "missing"),
    routeRef: route,
  };
  const fallback = await adapter.launch(context, { tracing: true });
  assert.deepEqual(fallback.env, {});
  assert.equal(fallback.close, undefined);
  let reason = "RUNTIME_OWNERSHIP_CONFLICT";
  const socketPath = path.join(f.root, "deny");
  const server = net.createServer((socket) =>
    socket.once("data", () =>
      socket.end(
        frame({
          protocol_version: 1,
          durable: false,
          code: 6,
          reason_code: reason,
        }),
      ),
    ),
  );
  await new Promise((r) => server.listen(socketPath, r));
  try {
    for (const code of [
      "RUNTIME_OWNERSHIP_CONFLICT",
      "NATIVE_POLICY_CONFLICT",
      "HELD_DISABLED",
      "HELD_AUTH",
      "INVALID_RESPONSE",
      "LOCAL_UNAVAILABLE",
    ]) {
      reason = code;
      await assert.rejects(
        adapter.launch({ ...context, socketPath }, { tracing: true }),
        new RegExp(code),
      );
    }
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT =
      "https://example.invalid/traces";
    await assert.rejects(
      adapter.launch(context, { tracing: true }),
      /NATIVE_DESTINATION_CONFLICT/,
    );
  } finally {
    await new Promise((r) => server.close(r));
  }
});

test("auth remove/add preserves binding keys and removes only FI authentication, even without installed routes", async (t) => {
  const f = fixture(t);
  await auth(f);
  const before = secrets(f),
    binding = loadConfig(f.p).profiles.fixture.binding;
  await f.ok(["auth", "remove", "--profile", "fixture"]);
  assert.deepEqual(secrets(f), {
    identity_key: before.identity_key,
    content_key: before.content_key,
  });
  assert.equal(loadConfig(f.p).profiles.fixture.removed, true);
  await auth(f, "rotated");
  assert.deepEqual(loadConfig(f.p).profiles.fixture.binding, binding);
  assert.equal(secrets(f).identity_key, before.identity_key);
  assert.equal(secrets(f).content_key, before.content_key);
  assert.equal(secrets(f).api_key, "rotated");
});

test("auth removal with stopped runtime fences routes and owned uninstall needs no credentials or installed binary", async (t) => {
  const f = fixture(t);
  await install(f);
  const before = secrets(f);
  await f.stop();
  await f.ok(["auth", "remove", "--profile", "fixture"]);
  assert.equal(loadConfig(f.p).routes[route].route.enabled, false);
  assert.equal(
    (await runtimeRequest(f.p.socket, { operation: "status" })).control_only,
    true,
  );
  await f.stop();
  fs.unlinkSync(f.binary);
  await f.ok(f.action("disable"));
  await f.ok(f.action("uninstall"));
  assert.deepEqual(loadConfig(f.p).routes, {});
  assert.deepEqual(secrets(f), {
    identity_key: before.identity_key,
    content_key: before.content_key,
  });
  const held = await f.cli(f.action("enable"));
  assert.equal(held.code, 3);
  assert.match(held.stdout, /HELD_AUTH/);
});

test("disable on stopped daemon persists its fence; later start never re-enables the route", async (t) => {
  const f = fixture(t);
  await install(f);
  await f.stop();
  await f.ok(f.action("disable"));
  assert.equal(loadConfig(f.p).routes[route].route.enabled, false);
  assert.equal(
    (await runtimeRequest(f.p.socket, { operation: "status" })).control_only,
    true,
  );
  await assert.rejects(
    runtimeRequest(f.p.socket, { operation: "native-open", route }),
    /HELD_DISABLED/,
  );
  await f.stop();
  await f.ok(["runtime", "start"]);
  assert.equal(
    (await runtimeRequest(f.p.socket, { operation: "status" })).agents[0]
      .enabled,
    false,
  );
  const run = await f.cli(f.action("run", "--", "--print", "fixture"));
  assert.equal(run.code, 3);
  assert.match(run.stdout, /ENABLE_REQUIRED/);
  await f.ok(f.action("enable"));
  assert.equal(
    (await runtimeRequest(f.p.socket, { operation: "status" })).control_only,
    false,
  );
});
