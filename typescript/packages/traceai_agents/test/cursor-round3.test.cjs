const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { spawnSync } = require("node:child_process");
const { cursorChange } = require("../dist/src/adapters/cursor/install.js");
const {
  cursorAdapters,
  HOOK_SCHEMA,
  normalizeCursorHook,
} = require("../dist/src/adapters/cursor/index.js");
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { Spool } = require("../dist/src/storage/store.js");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { FileSecretStore } = require("../dist/src/security/credentials.js");
const { paths, saveConfig } = require("../dist/src/config.js");
const { decodeTraces, encodeResponse } = require("../dist/src/export/otlp.js");
const { hook } = require("../contract/cursor-fixtures.cjs");
const base = path.resolve(__dirname, "../../../../../t");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const bin = path.resolve(__dirname, "../bin/fi-agent.cjs");
const binding = {
  id: "00000000-0000-4000-8000-000000000001",
  endpoint: "http://127.0.0.1:1/v1/traces",
  approvedOrigin: "http://127.0.0.1:1",
  project: "fixture",
  loopbackTest: true,
  backendContract: "immutable-agent-v1",
};
const route = {
  id: "cursor.ide",
  binding,
  policy: { revision: "1", categories: [], ceiling: [] },
  enabled: true,
  epoch: 1,
};
const secret = {
  api_key: "synthetic-api",
  secret_key: "synthetic-secret",
  identity_key: Buffer.alloc(32, 1).toString("hex"),
  content_key: Buffer.alloc(32, 2).toString("hex"),
};
const normalize = (e) =>
  normalizeCursorHook(e, "ide", {
    identityKey: Buffer.alloc(32, 1),
    policyRevision: "1",
    sourceSchema: HOOK_SCHEMA,
  })[0];
const spans = (bytes) =>
  decodeTraces(bytes).resourceSpans.flatMap((r) =>
    r.scopeSpans.flatMap((s) => s.spans),
  );
function fixture(t) {
  const root = fs.mkdtempSync(path.join(base, "r3-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}
const options = (root) => ({
  node: process.execPath,
  hookEntry: path.resolve(__dirname, "../bin/fi-cursor-hook.cjs"),
  hooksFile: path.join(root, "hooks.json"),
});
const context = (root) => ({
  routeRef: route.id,
  socketPath: path.join(root, "runtime.sock"),
  surface: "ide",
  profile: "fixture",
  binding,
  transcriptConsent: false,
  captureMode: "hooks",
  preview: true,
});

test("existing host config is refused instead of an unsafe replacing rename", async (t) => {
  const root = fixture(t),
    o = options(root),
    original = '{"version":1,"hooks":{},"editor":"original"}';
  fs.writeFileSync(o.hooksFile, original, { mode: 0o600 });
  let replacing = false;
  const rename = fs.renameSync;
  t.mock.method(fs, "renameSync", (from, to) => {
    if (to === o.hooksFile) {
      replacing = true;
      fs.writeFileSync(
        to,
        '{"version":1,"hooks":{"sessionStart":[{"command":"editor-hook"}]},"editor":"new"}',
      );
    }
    return rename(from, to);
  });
  await assert.rejects(
    async () =>
      (await cursorChange("enable", "ide", context(root), o)).commit(),
    /CURSOR_CONFIG_MANUAL_CLEANUP/,
  );
  assert.equal(
    replacing,
    false,
    "no replacing syscall is permitted for host-owned files",
  );
  assert.equal(fs.readFileSync(o.hooksFile, "utf8"), original);
});

test("terminal reconciliation is durable, bounded, replay-stable, and admits stop-before-tool", (t) => {
  const dir = path.join(fixture(t), "spool");
  let spool = new Spool(dir, "0.1.0");
  t.after(() => spool.close());
  spool.route(route);
  const now = Date.now;
  let clock = now();
  t.mock.method(Date, "now", () => clock);
  const admit = (e) =>
    spool.admit(route.id, normalize(e), undefined, true, 1, true);
  const stop = admit(hook("stop"));
  assert.equal(
    spool.next(route.id),
    undefined,
    "stop must not immediately lease its root",
  );
  clock += 500;
  assert.equal(admit(hook("stop")).duplicate, true);
  assert.throws(
    () => admit({ ...hook("stop"), status: "error" }),
    /HELD_IMMUTABLE_CONFLICT/,
  );
  admit(hook());
  const tool = spool.next(route.id);
  assert.ok(tool);
  spool.complete(tool.id, tool.epoch, { state: "accepted", code: "ACCEPTED" });
  spool.close();
  spool = new Spool(dir, "0.1.0");
  clock += 1499;
  assert.equal(spool.next(route.id), undefined);
  clock++;
  const parent = spool.next(route.id);
  assert.ok(parent, "replay must not extend the original 2-second grace");
  const root = spans(parent.bytes)[0],
    child = spans(tool.bytes)[0];
  assert.equal(root.spanId, child.parentSpanId);
  assert.ok(BigInt(root.startTimeUnixNano) <= BigInt(child.startTimeUnixNano));
  assert.ok(BigInt(root.endTimeUnixNano) >= BigInt(child.endTimeUnixNano));
  assert.equal(
    root.attributes.find((a) => a.key === "fi.agent.outcome").value.stringValue,
    "success",
  );
  spool.complete(parent.id, parent.epoch, {
    state: "accepted",
    code: "ACCEPTED",
  });
  assert.deepEqual(admit(hook("stop")), {
    receipt_id: stop.receipt_id,
    duplicate: true,
  });
  assert.throws(
    () => admit({ ...hook(), tool_use_id: "too-late" }),
    /HELD_TURN_FINALIZED/,
  );
});

test("runtime serializes source admission before asynchronous credential lookup", async (t) => {
  const root = fixture(t),
    received = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const b of req) chunks.push(b);
    received.push(...spans(Buffer.concat(chunks)));
    res.end(encodeResponse());
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => new Promise((r) => server.close(r)));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const upstream = {
    ...binding,
    endpoint: origin + "/v1/traces",
    approvedOrigin: origin,
  };
  const p = paths({
    FI_AGENT_CONFIG_HOME: path.join(root, "c"),
    FI_AGENT_STATE_HOME: path.join(root, "s"),
  });
  saveConfig(p, {
    schema_version: 1,
    profiles: {
      fixture: { binding: upstream, storage: "file", credentialRef: "fixture" },
    },
    routes: {
      [route.id]: {
        route: { ...route, binding: upstream },
        agent: "cursor",
        surface: "ide",
        profile: "fixture",
        transcriptConsent: false,
        captureMode: "hooks",
        activation: {
          installed: true,
          trusted: "unknown",
          activated: false,
          restartRequired: true,
        },
      },
    },
  });
  await new FileSecretStore(p.credentials).set("fixture", secret);
  const runtime = new Runtime(
    p,
    path.resolve(__dirname, "../dist/src/runtime/storage-child.js"),
    "0.1.0",
    new AdapterRegistry(cursorAdapters(options(root))),
  );
  await runtime.start();
  t.after(() => runtime.stop());
  const originalGet = FileSecretStore.prototype.get;
  let release,
    entered,
    calls = 0;
  const started = new Promise((r) => {
    entered = r;
  });
  const gate = new Promise((r) => {
    release = r;
  });
  t.mock.method(FileSecretStore.prototype, "get", async function (...args) {
    if (++calls === 1) {
      entered();
      await gate;
    }
    return originalGet.apply(this, args);
  });
  const observe = (e) =>
    runtimeRequest(
      p.socket,
      {
        operation: "observe-source",
        route_ref: route.id,
        source_schema: HOOK_SCHEMA,
        event: e,
      },
      5000,
    );
  const first = observe(hook());
  await started;
  const stop = observe(hook("stop"));
  // Wait for a later administrative request on the same runtime, not a scheduling guess.
  await runtimeRequest(p.socket, { operation: "status" });
  await new Promise((r) => setTimeout(r, 30));
  const beforeRelease = calls;
  release();
  assert.deepEqual(
    (await Promise.all([first, stop])).map((r) => r.durable),
    [true, true],
  );
  assert.equal(
    beforeRelease,
    1,
    "stop credential lookup must wait behind the earlier source",
  );
  await new Promise((r) => setTimeout(r, 2100));
  await runtimeRequest(
    p.socket,
    { operation: "flush", deadlineMs: 3000 },
    3500,
  );
  assert.equal(received.length, 2);
  assert.equal(
    received.filter((s) =>
      s.attributes.some(
        (a) => a.key === "fi.span.kind" && a.value.stringValue === "TOOL",
      ),
    ).length,
    1,
  );
  await runtime.stop();
});

function cli(root, args, input) {
  return spawnSync(process.execPath, [bin, ...args], {
    input,
    encoding: "utf8",
    timeout: 15000,
    env: {
      PATH: process.env.PATH,
      HOME: root,
      FI_AGENT_CONFIG_HOME: path.join(root, "c"),
      FI_AGENT_STATE_HOME: path.join(root, "s"),
    },
  });
}
for (const condition of ["auth-removed", "runtime-stopped", "profile-absent"]) {
  test(`cleanup fences capture without upstream auth or a running daemon: ${condition}`, async (t) => {
    const root = fs.mkdtempSync(path.join(base, "r3-"));
    t.after(() => {
      cli(root, ["runtime", "stop"]);
      fs.rmSync(root, { recursive: true, force: true });
    });
    const auth = cli(
      root,
      [
        "auth",
        "add",
        "--profile",
        "fixture",
        "--project",
        "fixture",
        "--stdin",
        "--storage",
        "file",
      ],
      JSON.stringify({
        api_key: "synthetic-api",
        secret_key: "synthetic-secret",
      }),
    );
    assert.equal(auth.status, 0, auth.stdout);
    assert.equal(
      cli(root, [
        "enable",
        "cursor",
        "--surface",
        "ide",
        "--profile",
        "fixture",
        "--preview",
      ]).status,
      0,
    );
    const configFile = path.join(root, "c/config.json"),
      hooksFile = path.join(root, ".cursor/hooks.json");
    const before = fs.readFileSync(hooksFile, "utf8");
    if (condition === "auth-removed")
      assert.equal(
        cli(root, ["auth", "remove", "--profile", "fixture"]).status,
        0,
      );
    if (condition === "runtime-stopped") {
      assert.equal(cli(root, ["runtime", "stop"]).status, 0);
      for (
        let i = 0;
        i < 100 && fs.existsSync(path.join(root, "s/runtime.sock"));
        i++
      )
        await new Promise((r) => setTimeout(r, 20));
      assert.equal(fs.existsSync(path.join(root, "s/runtime.sock")), false);
    }
    if (condition === "profile-absent") {
      const c = JSON.parse(fs.readFileSync(configFile));
      delete c.profiles.fixture;
      fs.writeFileSync(configFile, JSON.stringify(c));
    }
    for (const action of ["disable", "uninstall"]) {
      const result = cli(root, [action, "cursor", "--surface", "ide"]);
      assert.equal(result.status, 4, result.stdout + result.stderr);
      assert.match(result.stdout, /CURSOR_CONFIG_MANUAL_CLEANUP/);
      assert.equal(
        JSON.parse(fs.readFileSync(configFile)).routes[route.id].route.enabled,
        false,
      );
      assert.equal(
        fs.readFileSync(hooksFile, "utf8"),
        before,
        "no unsafe replacement even during cleanup",
      );
      await assert.rejects(
        runtimeRequest(path.join(root, "s/runtime.sock"), {
          operation: "observe-source",
          route_ref: route.id,
          source_schema: HOOK_SCHEMA,
          event: hook(),
        }),
        /HELD_DISABLED/,
      );
    }
    // User edits the host config themselves. Retained ownership supports the final no-op cleanup.
    fs.writeFileSync(
      hooksFile,
      '{"version":1,"hooks":{"sessionStart":[{"command":"keep"}]}}',
    );
    const removed = cli(root, ["uninstall", "cursor", "--surface", "ide"]);
    assert.equal(removed.status, 0, removed.stdout);
    assert.equal(
      JSON.parse(fs.readFileSync(configFile)).routes[route.id],
      undefined,
    );
    assert.match(fs.readFileSync(hooksFile, "utf8"), /keep/);
  });
}

test("failed daemon recovery retains the durable disable instead of re-enabling on restart", async (t) => {
  const root = fs.mkdtempSync(path.join(base, "r3-"));
  t.after(() => {
    cli(root, ["runtime", "stop"]);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const p = paths({
    FI_AGENT_CONFIG_HOME: path.join(root, "c"),
    FI_AGENT_STATE_HOME: path.join(root, "s"),
  });
  saveConfig(p, {
    schema_version: 1,
    profiles: {},
    routes: {
      [route.id]: {
        route,
        agent: "cursor",
        surface: "ide",
        profile: "missing",
        transcriptConsent: false,
        captureMode: "hooks",
        activation: {
          installed: true,
          trusted: "unknown",
          activated: false,
          restartRequired: false,
        },
      },
    },
  });
  const entry = path.resolve(__dirname, "../dist/src/cli/main.js");
  const result = spawnSync(
    process.execPath,
    [
      "-e",
      `require(${JSON.stringify(entry)}).main(['disable','cursor','--surface','ide'], {version:'0.1.0',entry:${JSON.stringify(path.join(root, "absent-entry.cjs"))},storageEntry:''}).then(code=>process.exitCode=code)`,
    ],
    {
      encoding: "utf8",
      timeout: 15000,
      env: {
        PATH: process.env.PATH,
        HOME: root,
        FI_AGENT_CONFIG_HOME: p.config,
        FI_AGENT_STATE_HOME: p.state,
      },
    },
  );
  assert.equal(result.status, 7, result.stdout);
  assert.match(result.stdout, /RUNTIME_UNAVAILABLE/);
  assert.equal(
    JSON.parse(fs.readFileSync(p.configFile)).routes[route.id].route.enabled,
    false,
  );
  assert.equal(cli(root, ["runtime", "start"]).status, 0);
  await assert.rejects(
    runtimeRequest(p.socket, {
      operation: "observe-source",
      route_ref: route.id,
      source_schema: HOOK_SCHEMA,
      event: hook(),
    }),
    /HELD_DISABLED/,
  );
});

test("v2 pending parents migrate without losing work and v3 finalization keeps the capture fence", (t) => {
  const Database = require("better-sqlite3");
  const dir = path.join(fixture(t), "spool");
  let spool = new Spool(dir, "0.1.0");
  spool.route(route);
  const first = spool.admit(
    route.id,
    normalize(hook()),
    undefined,
    true,
    1,
    true,
  );
  spool.close();
  // Recreate the prior schema on this synthetic DB, including its pending parent.
  const db = new Database(path.join(dir, "spool.db"));
  db.exec(
    "ALTER TABLE observed_turns DROP COLUMN terminal; PRAGMA user_version=2; UPDATE meta SET value='2' WHERE key='schema';",
  );
  db.close();
  spool = new Spool(dir, "0.1.0");
  t.after(() => spool.close());
  assert.deepEqual(
    spool.admit(route.id, normalize(hook()), undefined, true, 1, true),
    { receipt_id: first.receipt_id, duplicate: true },
  );
  spool.admit(route.id, normalize(hook("stop")), undefined, true, 1, true);
  spool.disable(route.id);
  const now = Date.now;
  t.mock.method(Date, "now", () => now() + 2001);
  assert.equal(spool.finalizeTurns(route.id), 0);
  assert.equal(spool.next(route.id), undefined);
  while (spool.purge(route.id)) {}
  assert.equal(spool.status().logical_bytes, 0);
});
