const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const net = require("node:net");
const { fork, spawn } = require("node:child_process");
const { once } = require("node:events");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { main } = require("../dist/src/cli/main.js");
const {
  paths,
  saveConfig,
  loadConfig,
  configTransaction,
} = require("../dist/src/config.js");
const {
  FileSecretStore,
  newCredentials,
} = require("../dist/src/security/credentials.js");
const { encodeResponse, decodeTraces } = require("../dist/src/export/otlp.js");
const { settings } = require("./fixtures/cli.cjs");
const reference = require("../contract/reference.cjs");
const base = path.resolve(__dirname, "../../../../../");
const fixtureEntry = path.resolve(__dirname, "fixtures/cli.cjs");
const fact = (id) =>
  reference.normalize(
    {
      schema: "fixture-v1",
      session: "s",
      turn: "t",
      id,
      unit: "request",
    },
    { identityKey: Buffer.alloc(32), policyRevision: "1" },
  )[0];
const routeId = "reference.fixture";
const cleanups = new WeakMap();
function cleanup(t, fn) {
  if (!cleanups.has(t)) {
    cleanups.set(t, []);
    t.after(async () => {
      for (const close of cleanups.get(t).reverse()) await close();
    });
  }
  cleanups.get(t).push(fn);
}

function fixture(t, bindingExtra = {}) {
  const root = fs.mkdtempSync(path.join(base, "rx-"));
  const env = {
    PATH: process.env.PATH,
    HOME: root,
    FI_AGENT_CONFIG_HOME: path.join(root, "c"),
    FI_AGENT_STATE_HOME: path.join(root, "s"),
  };
  const p = paths(env);
  const binding = {
    id: "00000000-0000-4000-8000-000000000001",
    endpoint: "https://example.test/v1/traces",
    approvedOrigin: "https://example.test",
    project: "fixture",
    ...bindingExtra,
  };
  const config = {
    schema_version: 1,
    profiles: { work: { binding, storage: "file", credentialRef: "fixture" } },
    routes: {
      [routeId]: {
        route: {
          id: routeId,
          binding,
          policy: { revision: "1", categories: [], ceiling: [] },
          enabled: true,
          epoch: 1,
        },
        agent: "reference",
        surface: "fixture",
        profile: "work",
        transcriptConsent: true,
        activation: {
          installed: true,
          trusted: true,
          activated: false,
          restartRequired: false,
        },
      },
    },
  };
  saveConfig(p, config);
  const previous = { ...process.env };
  Object.assign(process.env, env);
  delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
  delete process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
  cleanup(t, () => {
    for (const key of Object.keys(process.env))
      if (!(key in previous)) delete process.env[key];
    Object.assign(process.env, previous);
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, env, p, config };
}
async function start(t, p) {
  const runtime = new Runtime(
    p,
    settings.storageEntry,
    "0.1.0",
    settings.registry,
  );
  await runtime.start();
  cleanup(t, () => runtime.stop());
  return runtime;
}
async function upstream(t, handler) {
  const server = http.createServer(handler);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  cleanup(t, async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  return {
    endpoint: origin + "/v1/traces",
    approvedOrigin: origin,
    loopbackTest: true,
    backendContract: "immutable-agent-v1",
  };
}
async function credentials(p) {
  await new FileSecretStore(p.credentials).set(
    "fixture",
    newCredentials({
      api_key: "synthetic-api",
      secret_key: "synthetic-secret",
    }),
  );
}
test("import refuses a profile different from the installed route before reading a source or key", async (t) => {
  const { p, config, env } = fixture(t);
  config.profiles.other = {
    ...config.profiles.work,
    binding: {
      ...config.profiles.work.binding,
      id: "00000000-0000-4000-8000-000000000002",
      project: "other",
    },
    credentialRef: "no-key",
  };
  saveConfig(p, config);
  const result = await cli(env, [
    "import",
    "reference",
    "--surface",
    "fixture",
    "--profile",
    "other",
    "--project",
    "other",
    "--path",
    "/not-a-source",
    "--dry-run",
  ]);
  assert.equal(result.code, 3);
  assert.deepEqual(JSON.parse(result.stdout).reasons, [
    "PROJECT_BINDING_MISMATCH",
  ]);
  assert.equal(fs.existsSync(path.join(p.state, "import")), false);
});

test("storage-child failure stops observer readers as well as admission", async (t) => {
  const { p } = fixture(t);
  await credentials(p);
  let stops = 0;
  const { AdapterRegistry } = require("../dist/src/adapters.js");
  const registry = new AdapterRegistry([
    {
      ...reference,
      createObserver() {
        return {
          accept() {},
          status() {
            return {};
          },
          async stop() {
            stops++;
          },
        };
      },
    },
  ]);
  const runtime = new Runtime(p, settings.storageEntry, "0.1.0", registry);
  cleanup(t, () => runtime.stop());
  await runtime.start();
  const child = runtime.storage.child;
  child.kill("SIGKILL");
  await once(child, "exit");
  assert.equal(stops, 1);
  assert.equal(runtime.observers.size, 0);
});

const submit = (p, id, route = routeId) =>
  runtimeRequest(p.socket, {
    operation: "observe",
    route_ref: route,
    fact: { ...fact(id), surface: route.split(".")[1] },
  });
const flush = (p, deadlineMs = 5000) =>
  runtimeRequest(
    p.socket,
    { operation: "flush", deadlineMs },
    deadlineMs + 500,
  );
const command = (name, ...args) =>
  main([name, "reference", "--surface", "fixture", ...args], settings);
function cli(env, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fixtureEntry, ...args], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (b) => {
      stdout += b;
    });
    child.stderr.on("data", (b) => {
      stderr += b;
    });
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("fixture CLI timed out"));
    }, 10000);
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr });
    });
  });
}

test("unavailable telemetry launches without overlays and preserves child exit and signal", async (t) => {
  const { root, env } = fixture(t);
  const marker = path.join(root, "ran");
  const args = ["run", "reference", "--surface", "fixture", "--", "-e"];
  const result = await cli({ ...env, FIXTURE_UNAVAILABLE: "1" }, [
    ...args,
    `require('node:assert/strict').equal(process.env.OTEL_TRACES_EXPORTER, undefined); require('node:fs').writeFileSync(${JSON.stringify(marker)},'ran'); process.exit(23);`,
  ]);
  assert.equal(result.code, 23, JSON.stringify(result));
  assert.equal(fs.readFileSync(marker, "utf8"), "ran");
  assert.match(result.stderr, /launching without tracing/);
  const signaled = await cli({ ...env, FIXTURE_UNAVAILABLE: "1" }, [
    ...args,
    "process.kill(process.pid,'SIGTERM')",
  ]);
  assert.equal(signaled.signal, "SIGTERM", JSON.stringify(signaled));
  for (const extra of [
    { OTEL_EXPORTER_OTLP_ENDPOINT: "http://127.0.0.1:1" },
    { FIXTURE_CONFLICT: "1" },
    { FIXTURE_UNSAFE_OVERLAY: "1" },
  ]) {
    const refused = await cli({ ...env, FIXTURE_UNAVAILABLE: "1", ...extra }, [
      ...args,
      "process.exit(23)",
    ]);
    assert.equal(refused.code, 6, JSON.stringify(refused));
  }
});

test("installation-only update retains queued bytes and epoch, then exports all records", async (t) => {
  let uploads = 0;
  const binding = await upstream(t, (req, res) => {
    req.resume();
    uploads++;
    res.setHeader("content-type", "application/x-protobuf");
    res.end(encodeResponse());
  });
  const { p } = fixture(t, binding);
  await start(t, p); // No credentials yet: background cannot race the assertions.
  for (let i = 0; i < 5; i++) await submit(p, "update-" + i);
  const before = await flush(p);
  assert.equal(await command("update"), 0);
  assert.equal(loadConfig(p).routes[routeId].route.epoch, 1);
  const after = await flush(p);
  assert.equal(before.storage.held_disabled, 0);
  assert.equal(after.storage.held_disabled, 0);
  assert.equal(after.storage.logical_bytes, before.storage.logical_bytes);
  await credentials(p);
  const drained = await flush(p);
  assert.equal(uploads, 5);
  assert.equal(drained.storage.logical_bytes, 0);
  assert.equal(drained.persistence, "unknown");
});

test("uninstall/reinstall recovers retained generation and immutable receipts, including across restart", async (t) => {
  const { p } = fixture(t);
  let runtime = await start(t, p);
  await submit(p, "before-uninstall");
  assert.equal(await command("uninstall"), 0);
  assert.equal(loadConfig(p).routes[routeId], undefined);
  const retained = (
    await runtimeRequest(p.socket, { operation: "route-state", route: routeId })
  ).route;
  assert.equal(retained.enabled, false);
  assert.equal(retained.epoch, 2);
  await runtime.stop();
  runtime = await start(t, p);
  assert.equal(
    await command("enable", "--profile", "work", "--transcript", "structure"),
    0,
  );
  assert.equal(loadConfig(p).routes[routeId].route.epoch, 3);
  assert.equal((await submit(p, "after-reinstall")).durable, true);
  assert.equal((await submit(p, "before-uninstall")).reason_code, "DUPLICATE");
  assert.equal(await command("disable"), 0);
  const disabled = loadConfig(p).routes[routeId];
  assert.equal(await command("update"), 0);
  assert.deepEqual(loadConfig(p).routes[routeId].route, disabled.route);
});

test("reinstall destination refusal leaves no new registration or enabled config", async (t) => {
  const { p } = fixture(t);
  await start(t, p);
  await submit(p, "identity");
  assert.equal(await command("uninstall"), 0);
  const c = loadConfig(p);
  c.profiles.work.binding.project = "different-project";
  saveConfig(p, c);
  let committed = false;
  const { AdapterRegistry } = require("../dist/src/adapters.js");
  const registry = new AdapterRegistry([
    {
      ...reference,
      preflight: async () => ({
        commit: async () => {
          committed = true;
          return c.activation;
        },
        rollback: async () => {},
      }),
    },
  ]);
  const code = await main(
    [
      "enable",
      "reference",
      "--surface",
      "fixture",
      "--profile",
      "work",
      "--transcript",
      "structure",
    ],
    { ...settings, registry },
  );
  assert.equal(code, 3);
  assert.equal(committed, false);
  assert.equal(loadConfig(p).routes[routeId], undefined);
});

test("failed runtime application rolls back the saved config while holding its transaction", async (t) => {
  const { p } = fixture(t);
  const before = loadConfig(p);
  let attempts = 0;
  await assert.rejects(
    configTransaction(
      p,
      async (c) => {
        c.routes[routeId].route.enabled = false;
      },
      async () => {
        attempts++;
        if (attempts === 1) {
          assert.equal(loadConfig(p).routes[routeId].route.enabled, false);
          throw new Error("synthetic registration failure");
        }
        assert.deepEqual(loadConfig(p), before);
      },
    ),
    /synthetic registration failure/,
  );
  assert.equal(attempts, 2);
  assert.deepEqual(loadConfig(p), before);
});

test("runtime restarts after SIGKILL without stealing a live owner's socket", async (t) => {
  const { p, env } = fixture(t);
  const child = fork(fixtureEntry, ["__daemon"], {
    env,
    execPath: process.execPath,
    stdio: ["ignore", "ignore", "ignore", "ipc"],
  });
  cleanup(t, () => {
    if (child.exitCode === null) child.kill("SIGKILL");
  });
  const [ready] = await once(child, "message");
  assert.equal(ready.ready, true);
  await submit(p, "before-crash");
  const competitor = new Runtime(
    p,
    settings.storageEntry,
    "0.1.0",
    settings.registry,
  );
  await assert.rejects(competitor.start(), /RUNTIME_OWNERSHIP_CONFLICT/);
  assert.equal(
    (await runtimeRequest(p.socket, { operation: "status" })).state,
    "running",
  );
  const exited = once(child, "exit");
  child.kill("SIGKILL");
  await exited;
  const successors = [0, 1].map(
    () => new Runtime(p, settings.storageEntry, "0.1.0", settings.registry),
  );
  const starts = await Promise.allSettled(successors.map((r) => r.start()));
  assert.equal(
    starts.filter((r) => r.status === "fulfilled").length,
    1,
    JSON.stringify(starts),
  );
  const winner = successors[starts.findIndex((r) => r.status === "fulfilled")];
  cleanup(t, () => winner.stop());
  assert.equal((await submit(p, "before-crash")).reason_code, "DUPLICATE");
  assert.equal((await submit(p, "after-crash")).durable, true);
});

test("stale socket cleanup refuses ordinary files", async (t) => {
  const { p } = fixture(t);
  fs.mkdirSync(p.state, { mode: 0o700 });
  fs.writeFileSync(p.socket, "not a socket", { mode: 0o600 });
  const runtime = new Runtime(
    p,
    settings.storageEntry,
    "0.1.0",
    settings.registry,
  );
  await assert.rejects(runtime.start(), /UNSAFE_RUNTIME_SOCKET/);
  assert.equal(fs.readFileSync(p.socket, "utf8"), "not a socket");
});

test("flush honors a five-second IPC budget with a 1400ms healthy upstream", async (t) => {
  let uploads = 0;
  const binding = await upstream(t, (req, res) => {
    req.resume();
    uploads++;
    setTimeout(() => {
      res.setHeader("content-type", "application/x-protobuf");
      res.end(encodeResponse());
    }, 1400);
  });
  const { p } = fixture(t, binding);
  await credentials(p);
  await start(t, p);
  await submit(p, "slow");
  const started = Date.now();
  const result = await flush(p);
  assert.equal(uploads, 1);
  assert.ok(Date.now() - started >= 1300);
  assert.equal(result.storage.logical_bytes, 0);
  // Incomplete ingress still has the original short idle deadline.
  const socket = net.createConnection(p.socket);
  await once(socket, "connect");
  socket.write(Buffer.from([0]));
  await once(socket, "close");
});

test("flush drains fair multi-route bursts and respects retry and disable fences", async (t) => {
  const order = [];
  let reject = false;
  const binding = await upstream(t, async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const span = decodeTraces(Buffer.concat(chunks)).resourceSpans[0]
      .scopeSpans[0].spans[0];
    order.push(
      span.attributes.find((a) => a.key === "fi.agent.surface").value
        .stringValue,
    );
    res.setHeader("content-type", "application/x-protobuf");
    if (reject) res.writeHead(429, { "retry-after": "60" }).end();
    else res.end(encodeResponse());
  });
  const { p, config } = fixture(t, binding);
  const other = "reference.other";
  config.routes[other] = structuredClone(config.routes[routeId]);
  config.routes[other].route.id = other;
  config.routes[other].surface = "other";
  saveConfig(p, config);
  await start(t, p);
  for (let i = 0; i < 5; i++) {
    await submit(p, "a-" + i);
    await submit(p, "b-" + i, other);
  }
  await credentials(p);
  const result = await flush(p);
  assert.equal(order.length, 10);
  assert.deepEqual(order.slice(0, 4), ["fixture", "other", "fixture", "other"]);
  assert.equal(result.storage.logical_bytes, 0);
  reject = true;
  await submit(p, "retry");
  await flush(p);
  assert.equal(order.length, 11);
  await flush(p);
  assert.equal(order.length, 11); // next_at is not bypassed by another flush.
  await runtimeRequest(p.socket, { operation: "disable", route: routeId });
  reject = false;
  await flush(p);
  assert.equal(order.length, 11);
});

test("flush continues beyond a bounded 128-row batch", async (t) => {
  let uploads = 0;
  const binding = await upstream(t, (req, res) => {
    req.resume();
    uploads++;
    res.setHeader("content-type", "application/x-protobuf");
    res.end(encodeResponse());
  });
  const { p } = fixture(t, binding);
  await start(t, p);
  for (let i = 0; i < 130; i++) await submit(p, "batch-" + i);
  await credentials(p);
  const result = await flush(p, 10000);
  assert.equal(uploads, 130);
  assert.equal(result.storage.logical_bytes, 0);
});

test("flush deadline stays bounded and disable cancels the active sender", async (t) => {
  let uploads = 0;
  const binding = await upstream(t, (req, _res) => {
    req.resume();
    uploads++;
  });
  const { p } = fixture(t, binding);
  await credentials(p);
  await start(t, p);
  for (let i = 0; i < 3; i++) await submit(p, "stalled-" + i);
  const started = Date.now();
  const result = await flush(p, 100);
  assert.equal(result.code, 4);
  assert.ok(Date.now() - started < 1000);
  await runtimeRequest(p.socket, { operation: "disable", route: routeId });
  const disabledUploads = uploads;
  const pending = await flush(p);
  assert.equal(uploads, disabledUploads);
  assert.ok(pending.storage.logical_bytes > 0);
  await assert.rejects(submit(p, "disabled"), /HELD_DISABLED/);
});

test("an existing live listener is never reclaimed even without a storage owner", async (t) => {
  const { p } = fixture(t);
  fs.mkdirSync(p.state, { mode: 0o700 });
  const sockets = new Set();
  const listener = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise((r) => listener.listen(p.socket, r));
  fs.chmodSync(p.socket, 0o600);
  cleanup(t, async () => {
    for (const s of sockets) s.destroy();
    await new Promise((r) => listener.close(r));
  });
  const before = fs.statSync(p.socket).ino;
  const runtime = new Runtime(
    p,
    settings.storageEntry,
    "0.1.0",
    settings.registry,
  );
  await assert.rejects(runtime.start(), /RUNTIME_OWNERSHIP_CONFLICT/);
  assert.equal(fs.statSync(p.socket).ino, before);
});

for (const command of ["disable", "uninstall"])
  test(`${command} persists an offline fence and startup cannot resume capture`, async (t) => {
    const { p } = fixture(t);
    let runtime = await start(t, p);
    await runtime.stop();
    assert.equal(
      await main(
        [command, "reference", "--surface", "fixture", "--transcript", "off"],
        settings,
      ),
      0,
    );
    const config = loadConfig(p);
    assert.ok(config.fences[routeId]);
    if (command === "disable") {
      assert.equal(config.routes[routeId].route.enabled, false);
      assert.equal(config.routes[routeId].transcriptConsent, false);
    } else assert.equal(config.routes[routeId], undefined);
    runtime = await start(t, p);
    const stored = (
      await runtimeRequest(p.socket, {
        operation: "route-state",
        route: routeId,
      })
    ).route;
    assert.equal(stored.enabled, false);
    assert.equal(stored.epoch, 2);
    const ack = await require("@traceai/agent-core/client").observe(
      p.socket,
      routeId,
      fact("after"),
    );
    assert.equal(ack.durable, false);
    assert.equal(ack.reason_code, "HELD_DISABLED");
  });

test("offline purge is explicitly pending and reconciles before startup ingress", async (t) => {
  const { p } = fixture(t);
  let runtime = await start(t, p);
  await require("@traceai/agent-core/client").observe(
    p.socket,
    routeId,
    fact("queued"),
  );
  await runtime.stop();
  assert.equal(
    await main(
      ["disable", "reference", "--surface", "fixture", "--purge-capture"],
      settings,
    ),
    4,
  );
  runtime = await start(t, p);
  const route = (
    await runtimeRequest(p.socket, { operation: "route-state", route: routeId })
  ).route;
  assert.equal(route.enabled, false);
  // Explicit enable removes the tombstone but retains the durable generation.
  assert.equal(
    await main(["enable", "reference", "--surface", "fixture"], settings),
    0,
  );
  assert.equal(loadConfig(p).fences[routeId], undefined);
  assert.ok(
    (
      await runtimeRequest(p.socket, {
        operation: "route-state",
        route: routeId,
      })
    ).route.epoch > 2,
  );
});

test("Codex CLI check validates the same replacement as update; offline revocation persists registration too", async (t) => {
  const { root, p, config } = fixture(t);
  process.env.CODEX_HOME = path.join(root, "agent");
  const { codexAdapter } = require("../dist/src/adapters/codex/index.js");
  const {
    preflightPlugin,
    registration,
  } = require("../dist/src/adapters/codex/plugin.js");
  const { AdapterRegistry } = require("../dist/src/adapters.js");
  const hook = path.resolve(__dirname, "../bin/fi-agent-codex-hook.cjs");
  const id = "codex.cli",
    installed = config.routes[routeId];
  delete config.routes[routeId];
  config.routes[id] = {
    ...installed,
    agent: "codex",
    surface: "cli",
    route: { ...installed.route, id },
  };
  saveConfig(p, config);
  const context = {
    routeRef: id,
    socketPath: p.socket,
    surface: "cli",
    profile: "work",
    binding: installed.route.binding,
    transcriptConsent: true,
  };
  await preflightPlugin("enable", context, hook, "0.1.0").commit();
  const original = registration(p.socket);
  const adapter = codexAdapter(hook, "0.1.1");
  adapter.capability.platforms = [process.platform]; // fixture CLI contract on both CI hosts
  const registry = new AdapterRegistry([adapter]);
  const opts = { ...settings, registry, version: "0.1.1" };
  let runtime = new Runtime(p, settings.storageEntry, "0.1.1", registry);
  cleanup(t, () => runtime.stop());
  await runtime.start();
  const update = ["update", "codex", "--surface", "cli"];
  assert.equal(await main([...update, "--check"], opts), 0);
  assert.deepEqual(registration(p.socket), original);
  assert.equal(await main(update, opts), 0);
  const replacement = registration(p.socket);
  assert.notEqual(replacement.bundle, original.bundle);
  assert.equal(await main([...update, "--rollback", "--check"], opts), 0);
  assert.equal(await main([...update, "--rollback"], opts), 0);
  assert.equal(registration(p.socket).bundle, original.bundle);
  fs.appendFileSync(
    path.join(replacement.bundle, "codex/bin/observer.cjs"),
    "\n// conflicting immutable candidate\n",
  );
  assert.equal(await main([...update, "--check"], opts), 6);
  assert.equal(await main(update, opts), 6);
  assert.equal(registration(p.socket).bundle, original.bundle);
  await runtime.stop();
  assert.equal(
    await main(
      ["disable", "codex", "--surface", "cli", "--transcript", "off"],
      opts,
    ),
    0,
  );
  assert.equal(loadConfig(p).routes[id].transcriptConsent, false);
  assert.equal(loadConfig(p).routes[id].route.enabled, false);
  assert.equal(registration(p.socket).enabled, false);
  runtime = new Runtime(p, settings.storageEntry, "0.1.1", registry);
  await runtime.start();
  assert.equal(
    (await runtimeRequest(p.socket, { operation: "route-state", route: id }))
      .route.enabled,
    false,
  );
});
