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
// Keep Unix socket names below the platform limit even in deeply nested clones.
const base = fs.realpathSync(require("node:os").tmpdir());
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
    profiles: {
      work: { binding, storage: "file", credentialRef: "fixture" },
    },
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
    await runtimeRequest(p.socket, {
      operation: "route-state",
      route: routeId,
    })
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

test("run refuses a different approved profile before adapter launch or upload", async (t) => {
  let uploads = 0,
    launches = 0;
  const a = await upstream(t, (req, res) => {
    req.resume();
    uploads++;
    res.end();
  });
  const b = await upstream(t, (req, res) => {
    req.resume();
    uploads++;
    res.end();
  });
  const { p, config } = fixture(t, a);
  config.profiles.other = {
    storage: "file",
    credentialRef: "other",
    binding: {
      ...config.profiles.work.binding,
      ...b,
      id: "00000000-0000-4000-8000-000000000002",
      project: "other",
    },
  };
  saveConfig(p, config);
  await credentials(p);
  await start(t, p);
  const { AdapterRegistry } = require("../dist/src/adapters.js");
  const registry = new AdapterRegistry([
    {
      ...reference,
      launch: async () => {
        launches++;
        await submit(p, "wrong-destination");
        return { executable: process.execPath, args: [], env: {} };
      },
    },
  ]);
  const run = (profile) =>
    main(
      [
        "run",
        "reference",
        "--surface",
        "fixture",
        "--profile",
        profile,
        "--",
        "-e",
        "process.exit(0)",
      ],
      { ...settings, registry },
    );
  assert.equal(await run("other"), 3);
  assert.equal(launches, 0);
  await flush(p);
  assert.equal(uploads, 0);
  // Even the same profile name cannot launch with a changed binding.
  config.profiles.work.binding = {
    ...config.profiles.work.binding,
    project: "changed",
  };
  saveConfig(p, config);
  assert.equal(await run("work"), 3);
  assert.equal(launches, 0);
});

test("explicit transcript off refuses transcript-dependent re-enable and launch", async (t) => {
  const { p } = fixture(t);
  await start(t, p);
  assert.equal(await command("disable"), 0);
  const before = loadConfig(p);
  assert.equal(await command("enable", "--transcript", "off"), 3);
  assert.deepEqual(loadConfig(p), before);
  assert.equal(await command("enable"), 0); // Omitted retains prior approval.
  assert.equal(await command("enable", "--transcript", "off"), 3);
  assert.equal(
    await command("run", "--transcript", "off", "--", "-e", "process.exit(0)"),
    3,
  );
});

test("removed authentication still permits owned uninstall without restoring auth", async (t) => {
  const { p } = fixture(t);
  await credentials(p);
  await start(t, p);
  assert.equal(
    await main(["auth", "remove", "--profile", "work"], settings),
    0,
  );
  assert.equal(loadConfig(p).profiles.work.removed, true);
  assert.equal(await command("run", "--", "-e", "process.exit(0)"), 3);
  assert.equal(await command("uninstall"), 0);
  assert.equal(loadConfig(p).routes[routeId], undefined);
  assert.equal(loadConfig(p).profiles.work.removed, true);
});

for (const operation of ["disable", "uninstall", "auth-remove"])
  test(`stopped-runtime ${operation} fences persistent admission without draining`, async (t) => {
    let uploads = 0;
    const binding = await upstream(t, (req, res) => {
      req.resume();
      uploads++;
      res.end();
    });
    const { p } = fixture(t, binding);
    const runtime = await start(t, p);
    await submit(p, "offline-cleanup");
    await runtime.stop();
    await credentials(p);
    // Clean up a daemon even if a regression leaves the control runtime running.
    cleanup(t, async () => {
      try {
        await runtimeRequest(p.socket, { operation: "stop" });
      } catch {}
    });
    const code =
      operation === "auth-remove"
        ? await main(["auth", "remove", "--profile", "work"], settings)
        : await command(operation);
    assert.equal(code, 0);
    const installed = loadConfig(p).routes[routeId];
    assert.ok(
      operation === "uninstall"
        ? installed === undefined
        : installed.route.enabled === false,
    );
    await start(t, p);
    await flush(p);
    assert.equal(uploads, 0);
    await assert.rejects(submit(p, "after-offline-cleanup"), /HELD_DISABLED/);
  });

test("ambiguous response retries identical bytes after backoff across restart", async (t) => {
  const bodies = [];
  const binding = await upstream(t, async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    bodies.push(Buffer.concat(chunks));
    res.setHeader("content-type", "application/x-protobuf");
    res.end(encodeResponse(bodies.length === 1 ? 1 : 0));
  });
  const { p } = fixture(t, binding);
  await credentials(p);
  const runtime = await start(t, p);
  await submit(p, "ambiguous");
  const first = await flush(p);
  assert.equal(
    first.storage.counts.find((r) => r.state === "ambiguous").count,
    1,
  );
  await flush(p);
  assert.equal(bodies.length, 1);
  await runtime.stop();
  await start(t, p);
  await new Promise((r) => setTimeout(r, 1100));
  const recovered = await flush(p);
  assert.equal(bodies.length, 2);
  assert.deepEqual(bodies[1], bodies[0]);
  assert.equal(recovered.storage.logical_bytes, 0);
  assert.equal(recovered.persistence, "unknown");
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
    await runtimeRequest(p.socket, {
      operation: "route-state",
      route: routeId,
    })
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

test("Codex CLI check validates the same replacement as update; crash-released offline revocation persists registration too", async (t) => {
  const { root, p, config, env } = fixture(t);
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
  const owner = fork(path.resolve(__dirname, "fixtures/config-owner.cjs"), [], {
    env,
    execPath: process.execPath,
    stdio: ["ignore", "ignore", "ignore", "ipc"],
  });
  cleanup(t, () => owner.kill("SIGKILL"));
  assert.deepEqual((await once(owner, "message"))[0], { locked: true });
  const exited = once(owner, "exit");
  owner.kill("SIGKILL");
  await exited;
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
    (
      await runtimeRequest(p.socket, {
        operation: "route-state",
        route: id,
      })
    ).route.enabled,
    false,
  );
});

test("configuration ownership is crash-released and privacy revocation survives restart", async (t) => {
  const { p, env } = fixture(t);
  const runtime = await start(t, p);
  const owner = fork(path.resolve(__dirname, "fixtures/config-owner.cjs"), [], {
    env,
    execPath: process.execPath,
    stdio: ["ignore", "ignore", "ignore", "ipc"],
  });
  cleanup(t, () => owner.kill("SIGKILL"));
  assert.deepEqual((await once(owner, "message"))[0], { locked: true });
  await assert.rejects(
    configTransaction(p, async () => {}),
    /CONFIGURATION_CONFLICT/,
  );
  assert.equal(loadConfig(p).routes[routeId].route.enabled, true);
  const exited = once(owner, "exit");
  owner.kill("SIGKILL");
  await exited;
  assert.equal(await command("disable", "--transcript", "off"), 0);
  assert.equal(loadConfig(p).routes[routeId].transcriptConsent, false);
  assert.ok(loadConfig(p).fences[routeId]);
  await runtime.stop();
  await start(t, p);
  await assert.rejects(submit(p, "post-kill"), /HELD_DISABLED/);
});

test("configuration lock excludes same-process contenders without releasing the kernel owner", async (t) => {
  const { p, env } = fixture(t);
  await configTransaction(p, async () => {
    await assert.rejects(
      configTransaction(p, async () => {}),
      /CONFIGURATION_CONFLICT/,
    );
    const contender = fork(
      path.resolve(__dirname, "fixtures/config-owner.cjs"),
      [],
      {
        env,
        execPath: process.execPath,
        stdio: ["ignore", "ignore", "ignore", "ipc"],
      },
    );
    cleanup(t, () => contender.kill("SIGKILL"));
    assert.deepEqual((await once(contender, "message"))[0], {
      held: "CONFIGURATION_CONFLICT",
    });
  });
  const file = path.join(p.config, "write.lock"),
    first = fs.statSync(file);
  assert.equal(first.size, 0);
  assert.equal(first.mode & 0o777, 0o600);
  await configTransaction(p, async () => {});
  assert.equal(fs.statSync(file).ino, first.ino);
  assert.deepEqual(fs.readdirSync(p.config).sort(), [
    "config.json",
    "write.lock",
  ]);
});

test("simultaneous configuration starters acquire exactly one crash-released owner", async (t) => {
  const { p, env } = fixture(t);
  const owners = [];
  const results = await Promise.all(
    [0, 1, 2].map(async () => {
      const owner = fork(
        path.resolve(__dirname, "fixtures/config-owner.cjs"),
        [],
        {
          env,
          execPath: process.execPath,
          stdio: ["ignore", "ignore", "ignore", "ipc"],
        },
      );
      owners.push(owner);
      cleanup(t, () => owner.kill("SIGKILL"));
      return (await once(owner, "message"))[0];
    }),
  );
  assert.equal(results.filter((r) => r.locked).length, 1);
  assert.equal(
    results.filter((r) => r.held === "CONFIGURATION_CONFLICT").length,
    2,
  );
  const winner = owners[results.findIndex((r) => r.locked)];
  const exited = once(winner, "exit");
  winner.kill("SIGKILL");
  await exited;
  await configTransaction(p, async () => {});
});

test("legacy and unsafe configuration locks are refused without deleting unknown owners", async (t) => {
  const { root, p } = fixture(t);
  const file = path.join(p.config, "write.lock");
  fs.mkdirSync(file, { mode: 0o700 });
  await assert.rejects(
    configTransaction(p, async () => {}),
    /LEGACY_CONFIG_LOCK_REQUIRES_RECOVERY/,
  );
  assert.ok(fs.statSync(file).isDirectory());
  fs.rmdirSync(file);
  const target = path.join(root, "untouched");
  fs.writeFileSync(target, "fixture", { mode: 0o600 });
  fs.symlinkSync(target, file);
  await assert.rejects(configTransaction(p, async () => {}));
  assert.equal(fs.readFileSync(target, "utf8"), "fixture");
  fs.unlinkSync(file);
  fs.writeFileSync(file, "not empty", { mode: 0o600 });
  await assert.rejects(
    configTransaction(p, async () => {}),
    /UNSAFE_CONFIG_LOCK/,
  );
});

test("legacy unversioned runtime cannot certify a package upgrade", async (t) => {
  const { p } = fixture(t);
  fs.mkdirSync(p.state, { mode: 0o700 });
  const listener = net.createServer((socket) => {
    socket.once("data", () =>
      socket.end(
        require("@traceai/agent-core/client").frame({
          state: "running",
          code: 0,
        }),
      ),
    );
  });
  await new Promise((resolve) => listener.listen(p.socket, resolve));
  fs.chmodSync(p.socket, 0o600);
  cleanup(t, () => new Promise((resolve) => listener.close(resolve)));
  assert.equal(await main(["runtime", "start"], settings), 3);
  assert.equal(await command("update", "--check"), 3);
  assert.equal(await command("update"), 3);
});

test("package upgrades refuse a different running runtime before check or mutation", async (t) => {
  const { p } = fixture(t);
  const old = await start(t, p);
  const before = loadConfig(p);
  const candidate = { ...settings, version: "0.1.1" };
  for (const args of [
    ["runtime", "start"],
    ["update", "reference", "--surface", "fixture", "--check"],
    ["update", "reference", "--surface", "fixture"],
  ])
    assert.equal(await main(args, candidate), 3, args.join(" "));
  assert.deepEqual(loadConfig(p), before);
  // Administrative holds must not change the launched agent's exit semantics.
  assert.equal(
    await main(
      ["run", "reference", "--surface", "fixture", "--", "-e",
        "require('node:assert/strict').equal(process.env.OTEL_TRACES_EXPORTER, undefined); process.exit(23)"],
      candidate,
    ),
    23,
  );
  await old.stop();
  const upgraded = new Runtime(
    p,
    settings.storageEntry,
    "0.1.1",
    settings.registry,
  );
  cleanup(t, () => upgraded.stop());
  await upgraded.start();
  assert.equal(
    (await runtimeRequest(p.socket, { operation: "status" })).runtime_version,
    "0.1.1",
  );
  for (const extra of [["--check"], []])
    assert.equal(
      await main(
        ["update", "reference", "--surface", "fixture", ...extra],
        candidate,
      ),
      0,
    );
});

for (const offline of [false, true])
  test(`contract approval migrates installed and stored capabilities without changing work identity (offline=${offline})`, async (t) => {
    const binding = await upstream(t, (req, res) => {
      req.resume();
      res.setHeader("content-type", "application/x-protobuf");
      res.end(encodeResponse());
    });
    delete binding.backendContract;
    const { root, p, config, env } = fixture(t, binding);
    // Disabled routes must receive the capability too, without being re-enabled.
    config.routes["reference.other"] = structuredClone(config.routes[routeId]);
    config.routes["reference.other"].surface = "other";
    config.routes["reference.other"].route.id = "reference.other";
    config.routes["reference.other"].route.enabled = false;
    saveConfig(p, config);
    await credentials(p);
    let runtime = await start(t, p);
    const receipt = await submit(p, "before-contract");
    if (offline) await runtime.stop();
    const contract = path.join(root, "contract.json"),
      auth = path.join(root, "synthetic-auth.json");
    fs.writeFileSync(
      contract,
      JSON.stringify({
        contract: "immutable-agent-v1",
        endpoint: binding.endpoint,
        project: "fixture",
        immutableAdmission: true,
        uniqueReads: true,
        usagePlans: true,
      }),
      { mode: 0o600 },
    );
    fs.writeFileSync(
      auth,
      JSON.stringify({
        api_key: "synthetic-api",
        secret_key: "synthetic-secret",
      }),
      { mode: 0o600 },
    );
    const approved = await cli(env, [
      "auth",
      "add",
      "--profile",
      "work",
      "--project",
      "fixture",
      "--credential-file",
      auth,
      "--contract-manifest",
      contract,
      "--loopback-test",
    ]);
    assert.equal(approved.code, 0, approved.stdout);
    if (offline) runtime = await start(t, p);
    const updated = loadConfig(p);
    for (const id of [routeId, "reference.other"]) {
      assert.equal(
        updated.routes[id].route.binding.backendContract,
        "immutable-agent-v1",
      );
      const stored = (
        await runtimeRequest(p.socket, {
          operation: "route-state",
          route: id,
        })
      ).route;
      assert.deepEqual(stored, updated.routes[id].route);
      assert.equal(stored.epoch, 1);
    }
    assert.equal(updated.routes["reference.other"].route.enabled, false);
    assert.equal(
      (await submit(p, "before-contract")).receipt_id,
      receipt.receipt_id,
    );
    assert.equal((await flush(p)).storage.logical_bytes, 0);
    assert.equal(await command("update", "--check"), 0);
    assert.equal(await command("update"), 0);
    await runtime.stop();
    await start(t, p);
    assert.equal((await submit(p, "before-contract")).reason_code, "DUPLICATE");
  });

test("update check validates the prospective destination, not the installed route", async (t) => {
  const { p } = fixture(t);
  await start(t, p);
  const c = loadConfig(p);
  c.profiles.work.binding.project = "changed-destination";
  saveConfig(p, c);
  assert.equal(await command("update", "--check"), 3);
  assert.equal(await command("update"), 3);
  assert.equal(loadConfig(p).routes[routeId].route.binding.project, "fixture");
});

test("startup retries a released transient SQLite owner with a fresh connection", async (t) => {
  const { p } = fixture(t);
  const { Spool } = require("../dist/src/storage/store.js");
  const root = path.join(p.state, "spool");
  const initial = new Spool(root, "0.1.0");
  initial.close();
  const reader = new (require("better-sqlite3"))(path.join(root, "spool.db"));
  cleanup(t, () => { if (reader.open) reader.close(); });
  reader.pragma("journal_mode=DELETE");
  reader.exec("BEGIN; SELECT count(*) FROM meta;");
  const runtime = new Runtime(p, path.resolve(__dirname, "fixtures/ownership-retry.cjs"), "0.1.0", settings.registry);
  cleanup(t, () => runtime.stop());
  const started = runtime.start();
  // Hold the real reader until the first actual lock acquisition fails. No
  // weakened assertion or arbitrary delay: restart must recover after release.
  const busy = new Promise((resolve) => runtime.storage.child.on("message", (message) => {
    if (message.probe === "busy") { reader.close(); resolve(); }
  }));
  await busy;
  await started;
  assert.equal((await submit(p, "after-contention")).durable, true);
});

test("storage ownership precedes header inspection, preventing two-reader upgrade deadlock", async (t) => {
  const { root, p, env } = fixture(t);
  const { Spool } = require("../dist/src/storage/store.js");
  const spoolRoot = path.join(p.state, "spool");
  const initial = new Spool(spoolRoot, "0.1.0");
  initial.close();
  // New SQLite databases begin in rollback-journal mode; establish that state
  // deterministically rather than depending on startup scheduler interleaving.
  const database = new (require("better-sqlite3"))(
    path.join(spoolRoot, "spool.db"),
  );
  database.pragma("journal_mode=DELETE");
  database.close();
  const release = path.join(root, "release-barrier");
  const entry = path.resolve(__dirname, "fixtures/ownership.cjs");
  const spawnOwner = () => {
    const child = fork(entry, [spoolRoot, release], {
      env,
      execPath: process.execPath,
      stdio: ["ignore", "ignore", "ignore", "ipc"],
    });
    cleanup(t, () => {
      child.kill("SIGKILL");
    });
    return child;
  };
  const first = spawnOwner();
  assert.equal((await once(first, "message"))[0].phase, "inspection");
  // Keep the first process paused after its first header read. A second owner
  // must fail ownership BEFORE it can reach that read and retain a SHARED lock.
  const second = spawnOwner();
  const [result] = await once(second, "message");
  const ready = once(first, "message");
  fs.writeFileSync(release, "release", { mode: 0o600 });
  assert.deepEqual(result, {
    phase: "held",
    code: "RUNTIME_OWNERSHIP_CONFLICT",
  });
  assert.equal((await ready)[0].phase, "ready");
});

test("auth remove fences capture and strips upstream keys even when local socket is unresponsive", async (t) => {
  const { p } = fixture(t);
  await credentials(p);
  fs.mkdirSync(p.state, { recursive: true, mode: 0o700 });
  const sockets = new Set();
  const listener = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise((resolve) => listener.listen(p.socket, resolve));
  fs.chmodSync(p.socket, 0o600);
  cleanup(t, async () => {
    if (listener.listening) {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => listener.close(resolve));
    }
  });
  assert.equal(await main(["auth", "remove", "--profile", "work"], settings), 0);
  const config = loadConfig(p);
  assert.equal(config.profiles.work.removed, true);
  assert.equal(config.routes[routeId].route.enabled, false);
  assert.ok(config.fences[routeId]);
  const secret = await new FileSecretStore(p.credentials).get("fixture");
  assert.ok(secret.identity_key);
  assert.equal(secret.api_key, undefined);
  assert.equal(secret.secret_key, undefined);
  // A restart must enforce the persisted fence before opening ingress.
  for (const socket of sockets) socket.destroy();
  await new Promise((resolve) => listener.close(resolve));
  const restarted = await start(t, p);
  const stored = (
    await runtimeRequest(p.socket, {
      operation: "route-state",
      route: routeId,
    })
  ).route;
  assert.equal(stored.enabled, false);
  assert.equal(stored.epoch, 2);
  await assert.rejects(submit(p, "after-auth-remove"), /HELD_DISABLED/);
  await restarted.stop();
});

test("managed run launches child promptly when local socket is unresponsive", async (t) => {
  const { p } = fixture(t);
  fs.mkdirSync(p.state, { recursive: true, mode: 0o700 });
  const sockets = new Set();
  const listener = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise((resolve) => listener.listen(p.socket, resolve));
  fs.chmodSync(p.socket, 0o600);
  cleanup(t, async () => {
    if (listener.listening) {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => listener.close(resolve));
    }
  });
  const started = Date.now();
  assert.equal(await command("run", "--", "-e", "process.exit(5)"), 5);
  assert.ok(
    Date.now() - started < 1500,
    "tracing must not block child launch for seconds",
  );
});

test("runtime start reports a too-long socket path before detaching", async (t) => {
  const { root, env } = fixture(t);
  const longState = path.join(root, "s".repeat(105));
  const result = await cli(
    { ...env, FI_AGENT_STATE_HOME: longState },
    ["runtime", "start"],
  );
  assert.equal(result.code, 7);
  assert.deepEqual(JSON.parse(result.stdout).reasons, ["SOCKET_PATH_TOO_LONG"]);
  assert.equal(fs.existsSync(longState), false);
});

test("backend approval updates an installed route without losing its identity", async (t) => {
  const { root, p } = fixture(t);
  await credentials(p);
  await start(t, p);
  const before = loadConfig(p).routes[routeId].route;
  const auth = path.join(root, "auth.json"), manifest = path.join(root, "manifest.json");
  fs.writeFileSync(auth, JSON.stringify({api_key:"synthetic-api",secret_key:"synthetic-secret"}), {mode:0o600});
  fs.writeFileSync(manifest, JSON.stringify({contract:"immutable-agent-v1",endpoint:before.binding.endpoint,project:before.binding.project,immutableAdmission:true,uniqueReads:true,usagePlans:true}), {mode:0o600});
  assert.equal(await main(["auth","add","--profile","work","--project","fixture","--credential-file",auth,"--contract-manifest",manifest],settings), 0);
  const after = loadConfig(p).routes[routeId].route;
  assert.equal(after.binding.backendContract, "immutable-agent-v1");
  assert.equal(after.epoch, before.epoch);
  assert.equal((await runtimeRequest(p.socket,{operation:"route-state",route:routeId})).route.binding.backendContract,"immutable-agent-v1");
  assert.equal(await command("update"),0);
  assert.equal(await command("disable"),0);
  assert.equal(await command("uninstall"),0);
});

test("failed owned cleanup keeps durable fence restartable and removable", async (t) => {
  const {p}=fixture(t);
  let runtime=await start(t,p);
  const {AdapterRegistry}=require("../dist/src/adapters.js");
  let fail=true;
  const registry=new AdapterRegistry([{...reference,preflight:async()=>({commit:async()=>{if(fail) throw Error("synthetic commit failure"); return loadConfig(p).routes[routeId].activation;},rollback:async()=>{}})}]);
  assert.equal(await main(["disable","reference","--surface","fixture"],{...settings,registry}),7);
  assert.equal(loadConfig(p).routes[routeId].route.enabled,false);
  await runtime.stop();
  runtime=await start(t,p);
  assert.equal((await runtimeRequest(p.socket,{operation:"route-state",route:routeId})).route.enabled,false);
  fail=false;
  assert.equal(await main(["uninstall","reference","--surface","fixture"],{...settings,registry}),0);
});

test("inaccessible telemetry state still launches the child without tracing", async (t) => {
  const {root,env}=fixture(t);
  const marker=path.join(root,"launched");
  const bad=path.join(root,"not-a-directory");
  fs.writeFileSync(bad,"blocked");
  const result=await cli({...env,FI_AGENT_STATE_HOME:path.join(bad,"state")},["run","reference","--surface","fixture","--","-e",`require('node:fs').writeFileSync(${JSON.stringify(marker)},'yes'); process.exit(23)`]);
  assert.equal(result.code,23,JSON.stringify(result));
  assert.equal(fs.readFileSync(marker,"utf8"),"yes");
});

test("configuration lock survives live contention but releases on process death", async (t) => {
  const {p}=fixture(t);
  const modulePath=path.resolve(__dirname,"../dist/src/config.js");
  const script=`const {configTransaction}=require(${JSON.stringify(modulePath)}); globalThis.pending=configTransaction(${JSON.stringify(p)},async()=>{process.send('locked'); await new Promise(resolve=>process.on('message',resolve))})`;
  const worker=fork("-e",[script],{execArgv:[],execPath:process.execPath,stdio:["ignore","ignore","pipe","ipc"]});
  let workerError="";
  worker.stderr.on("data",chunk=>{workerError+=chunk});
  cleanup(t,()=>{if(worker.exitCode===null) worker.kill("SIGKILL")});
  await once(worker,"message");
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(worker.exitCode,null,"lock owner exited before the contention check: "+workerError);
  await assert.rejects(configTransaction(p,async()=>{}),/CONFIGURATION_CONFLICT/);
  const exit=once(worker,"exit"); worker.kill("SIGKILL"); await exit;
  await configTransaction(p,async(c)=>{c.profiles.work.removed=true});
  assert.equal(loadConfig(p).profiles.work.removed,true);
});

test("runtime reconciles a crash after the storage fence but before config save", async (t) => {
  const {p}=fixture(t);
  const runtime=await start(t,p);
  await runtimeRequest(p.socket,{operation:"disable",route:routeId});
  assert.equal(loadConfig(p).routes[routeId].route.enabled,true);
  await runtime.stop();
  await start(t,p);
  assert.equal(loadConfig(p).routes[routeId].route.enabled,false);
  assert.equal(loadConfig(p).routes[routeId].route.epoch,2);
  assert.equal(await command("uninstall"),0);
});

test("failed uninstall and failed rollback keep durable cleanup fence", async (t) => {
  const {p}=fixture(t);
  const runtime=await start(t,p);
  const {AdapterRegistry}=require("../dist/src/adapters.js");
  const registry=new AdapterRegistry([{...reference,preflight:async()=>({commit:async()=>{throw Error("owned change failed")},rollback:async()=>{throw Error("rollback failed")}})}]);
  assert.equal(await main(["uninstall","reference","--surface","fixture"],{...settings,registry}),7);
  assert.equal(loadConfig(p).routes[routeId].route.enabled,false);
  assert.equal(loadConfig(p).routes[routeId].route.epoch,2);
  await runtime.stop();
  await start(t,p);
  assert.equal((await runtimeRequest(p.socket,{operation:"route-state",route:routeId})).route.enabled,false);
});

test("offline credential rotation without contract approval leaves routes usable", async (t) => {
  const {root,p}=fixture(t);
  await credentials(p);
  const auth=path.join(root,"rotation.json");
  fs.writeFileSync(auth,JSON.stringify({api_key:"synthetic-rotated",secret_key:"synthetic-secret"}),{mode:0o600});
  assert.equal(await main(["auth","add","--profile","work","--project","fixture","--credential-file",auth],settings),0);
  assert.equal(loadConfig(p).routes[routeId].route.enabled,true);
});
