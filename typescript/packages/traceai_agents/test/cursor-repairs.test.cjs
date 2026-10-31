const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const Database = require("better-sqlite3");
const { parse } = require("jsonc-parser");
const {
  cursorChange,
  cursorProbe,
} = require("../dist/src/adapters/cursor/install.js");
const {
  cursorAdapters,
  normalizeCursorHook,
  HOOK_SCHEMA,
} = require("../dist/src/adapters/cursor/index.js");
const {
  Spool,
  OBSERVED_TURN_WINDOW_MS,
} = require("../dist/src/storage/store.js");
const { AgentError } = require("../dist/src/security/files.js");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { saveConfig, paths } = require("../dist/src/config.js");
const { FileSecretStore } = require("../dist/src/security/credentials.js");
const { decodeTraces, encodeResponse } = require("../dist/src/export/otlp.js");
const { hook, canary } = require("../contract/cursor-fixtures.cjs");
const base = path.resolve(__dirname, "../../../../../t");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const binding = {
  id: "00000000-0000-4000-8000-000000000001",
  endpoint: "http://127.0.0.1:1/v1/traces",
  approvedOrigin: "http://127.0.0.1:1",
  project: "fixture",
  loopbackTest: true,
  backendContract: "immutable-agent-v1",
};
const normalizeContext = {
  identityKey: Buffer.alloc(32, 1),
  policyRevision: "1",
  sourceSchema: HOOK_SCHEMA,
};
const normalize = (event) =>
  normalizeCursorHook(event, "ide", normalizeContext)[0];
const route = {
  id: "cursor.ide",
  binding,
  policy: { revision: "1", categories: [], ceiling: [] },
  enabled: true,
  epoch: 1,
};
function fixture(t) {
  const root = fs.mkdtempSync(path.join(base, "r-"));
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
const spans = (bytes) =>
  decodeTraces(bytes).resourceSpans.flatMap((r) =>
    r.scopeSpans.flatMap((s) => s.spans),
  );
const attr = (s, key) => s.attributes.find((a) => a.key === key)?.value;

for (const action of ["enable", "disable", "rollback"]) {
  test(`${action} never stages a replacement for existing host configuration`, async (t) => {
    const root = fixture(t),
      o = options(root),
      c = context(root);
    let enable;
    if (action === "enable")
      fs.writeFileSync(o.hooksFile, '{"version":1,"hooks":{}}', {
        mode: 0o600,
      });
    else {
      enable = await cursorChange("enable", "ide", c, o);
      await enable.commit();
    }
    const before = fs.readFileSync(o.hooksFile, "utf8");
    let hostStaged = false;
    const original = fs.fsyncSync;
    t.mock.method(fs, "fsyncSync", (fd) => {
      for (const name of fs
        .readdirSync(root)
        .filter((n) => n.startsWith(".pending-")))
        if (fs.readFileSync(path.join(root, name), "utf8").includes('"hooks"'))
          hostStaged = true;
      return original(fd);
    });
    await assert.rejects(
      async () =>
        action === "rollback"
          ? enable.rollback()
          : (await cursorChange(action, "ide", c, o)).commit(),
      /CURSOR_CONFIG_MANUAL_CLEANUP/,
    );
    assert.equal(hostStaged, false);
    assert.equal(fs.readFileSync(o.hooksFile, "utf8"), before);
    assert.ok(!fs.readdirSync(root).some((n) => n.startsWith(".pending-")));
  });
}

test("same-text inode replacement and a newly created config are conflicts", async (t) => {
  const root = fixture(t),
    o = options(root),
    c = context(root);
  await (await cursorChange("enable", "ide", c, o)).commit();
  const text = fs.readFileSync(o.hooksFile, "utf8");
  const change = await cursorChange("enable", "ide", c, o); // qualified no-op
  const replacement = path.join(root, "editor-save");
  fs.writeFileSync(replacement, text, { mode: 0o600 });
  fs.renameSync(replacement, o.hooksFile);
  await assert.rejects(change.commit(), /CURSOR_CONFIG_CONFLICT/);
  assert.equal(fs.readFileSync(o.hooksFile, "utf8"), text);
  fs.unlinkSync(o.hooksFile);
  const absent = await cursorChange("enable", "ide", c, o);
  const link = fs.linkSync;
  t.mock.method(fs, "linkSync", (from, to) => {
    if (to === o.hooksFile) fs.writeFileSync(to, text, { mode: 0o600 });
    return link(from, to);
  });
  await assert.rejects(absent.commit(), /CURSOR_CONFIG_CONFLICT/);
  assert.equal(fs.readFileSync(o.hooksFile, "utf8"), text);
});

test("doctor and cleanup retain installed Node/package/socket ownership across upgrades", async (t) => {
  const root = fixture(t),
    o = options(root),
    c = context(root);
  const old = {
    ...o,
    node: "/synthetic/node22/bin/node",
    hookEntry: "/synthetic/package-v1/fi-cursor-hook.cjs",
  };
  const upgraded = {
    ...o,
    node: "/synthetic/node24/bin/node",
    hookEntry: "/synthetic/package-v2/fi-cursor-hook.cjs",
  };
  await (await cursorChange("enable", "ide", c, old)).commit();
  const newContext = {
    ...c,
    socketPath: path.join(root, "moved/runtime.sock"),
  };
  assert.equal(
    (await cursorProbe("ide", newContext, upgraded)).installed,
    true,
  );
  await assert.rejects(
    cursorChange("enable", "ide", newContext, upgraded),
    /CURSOR_CONFIG_CONFLICT/,
  );
  const d = parse(fs.readFileSync(o.hooksFile, "utf8"));
  d.hooks.sessionStart = [{ command: "unrelated" }];
  fs.writeFileSync(o.hooksFile, JSON.stringify(d));
  const before = fs.readFileSync(o.hooksFile, "utf8");
  for (const action of ["disable", "uninstall"])
    await assert.rejects(
      cursorChange(action, "ide", newContext, upgraded),
      /CURSOR_CONFIG_MANUAL_CLEANUP/,
    );
  assert.equal(fs.readFileSync(o.hooksFile, "utf8"), before);
  // A manual edit removes just the owned hooks. Cleanup then becomes a no-op.
  fs.writeFileSync(
    o.hooksFile,
    JSON.stringify({ ...d, hooks: { sessionStart: d.hooks.sessionStart } }),
  );
  await (await cursorChange("uninstall", "ide", newContext, upgraded)).commit();
  assert.equal(
    (await cursorProbe("ide", newContext, upgraded)).installed,
    false,
  );
  assert.deepEqual(parse(fs.readFileSync(o.hooksFile, "utf8")).hooks, {
    sessionStart: [{ command: "unrelated" }],
  });
  // Modified registrations remain ownership conflicts, not broad substring deletion.
  d.hooks.stop[0].timeout = 9;
  fs.writeFileSync(o.hooksFile, JSON.stringify(d));
  await assert.rejects(
    cursorChange("disable", "ide", newContext, upgraded),
    /CURSOR_CONFIG_CONFLICT/,
  );
});

function spoolFixture(t) {
  const dir = path.join(fixture(t), "spool");
  let spool = new Spool(dir, "0.1.0");
  spool.route(route);
  t.after(() => spool.close());
  return {
    get spool() {
      return spool;
    },
    dir,
    reopen() {
      spool.close();
      spool = new Spool(dir, "0.1.0");
    },
  };
}
const admit = (spool, event) =>
  spool.admit(route.id, normalize(event), undefined, true, 1, true);
function accept(spool) {
  const row = spool.next(route.id);
  assert.ok(row);
  const span = spans(row.bytes)[0];
  spool.complete(row.id, row.epoch, { state: "accepted", code: "ACCEPTED" });
  return span;
}

test("lost stop has a durable bounded incomplete root after restart; late work cannot rewrite it", (t) => {
  const f = spoolFixture(t);
  const now = Date.now;
  let clock = now();
  Date.now = () => clock;
  try {
    const first = admit(f.spool, hook());
    const tool = accept(f.spool);
    assert.equal(
      f.spool.next(route.id),
      undefined,
      "parent is pending, not prematurely exported",
    );
    f.reopen();
    clock += OBSERVED_TURN_WINDOW_MS - 1;
    assert.equal(f.spool.finalizeTurns(route.id), 0);
    clock++;
    const root = accept(f.spool);
    assert.equal(root.spanId, tool.parentSpanId);
    assert.equal(root.traceId, tool.traceId);
    assert.equal(root.startTimeUnixNano, tool.startTimeUnixNano);
    assert.equal(
      root.endTimeUnixNano,
      tool.endTimeUnixNano,
      "timeout is not the execution end",
    );
    assert.equal(attr(root, "fi.agent.outcome").stringValue, "incomplete");
    assert.equal(attr(root, "fi.capture.partial").boolValue, true);
    assert.equal(attr(root, "fi.capture.timing").stringValue, "estimated");
    assert.equal(f.spool.finalizeTurns(route.id), 0);
    assert.deepEqual(admit(f.spool, hook()), {
      receipt_id: first.receipt_id,
      duplicate: true,
    });
    assert.throws(
      () => admit(f.spool, hook("stop")),
      /HELD_IMMUTABLE_CONFLICT/,
    );
    assert.throws(
      () => admit(f.spool, { ...hook(), tool_use_id: "late-tool" }),
      /HELD_TURN_FINALIZED/,
    );
    assert.equal(f.spool.next(route.id), undefined);
  } finally {
    Date.now = now;
  }
});

test("normal stop bounds all observed tools and replays the same root without inventing source timing", (t) => {
  const f = spoolFixture(t),
    now = Date.now;
  let clock = now();
  Date.now = () => clock;
  try {
    admit(f.spool, hook());
    const tool1 = accept(f.spool);
    clock += 100;
    admit(f.spool, { ...hook(), tool_use_id: "tool-2", duration: 250 });
    const tool2 = accept(f.spool);
    clock += 100;
    const receipt = admit(f.spool, hook("stop"));
    assert.equal(f.spool.next(route.id), undefined);
    clock += 2000;
    const root = accept(f.spool);
    assert.equal(root.startTimeUnixNano, tool2.startTimeUnixNano);
    assert.ok(
      BigInt(root.startTimeUnixNano) <= BigInt(tool1.startTimeUnixNano),
    );
    assert.ok(BigInt(root.endTimeUnixNano) >= BigInt(tool2.endTimeUnixNano));
    assert.equal(attr(root, "fi.agent.outcome").stringValue, "success");
    assert.equal(attr(root, "fi.capture.partial").boolValue, true);
    clock += 1000;
    assert.deepEqual(admit(f.spool, hook("stop")), {
      receipt_id: receipt.receipt_id,
      duplicate: true,
    });
    assert.throws(
      () => admit(f.spool, { ...hook("stop"), status: "error" }),
      /HELD_IMMUTABLE_CONFLICT/,
    );
  } finally {
    Date.now = now;
  }
});

test("failed child admission rolls back its reserved parent; disable/purge fence pending roots", (t) => {
  const f = spoolFixture(t);
  // A parent fits, but parent + tool do not: the transaction must leave neither.
  const quota = f.spool.checkQuota;
  let checks = 0;
  f.spool.checkQuota = function (...args) {
    if (++checks === 2) throw new AgentError("PAUSED_LOGICAL_QUOTA");
    return quota.apply(this, args);
  };
  try {
    assert.throws(() => admit(f.spool, hook()), /PAUSED_LOGICAL_QUOTA/);
    assert.equal(checks, 2);
    assert.deepEqual(f.spool.status().counts, []);
  } finally {
    f.spool.checkQuota = quota;
  }
  admit(f.spool, hook());
  f.spool.disable(route.id);
  const now = Date.now;
  Date.now = () => now() + OBSERVED_TURN_WINDOW_MS + 1;
  try {
    assert.equal(f.spool.finalizeTurns(route.id), 0);
    assert.equal(f.spool.next(route.id), undefined);
  } finally {
    Date.now = now;
  }
  while (f.spool.purge(route.id)) {}
  assert.equal(f.spool.status().logical_bytes, 0);
  f.spool.route({ ...route, epoch: 3 });
  assert.throws(
    () => f.spool.admit(route.id, normalize(hook()), undefined, true, 1, true),
    /STALE_CAPTURE_EPOCH/,
  );
});

test("real runtime and SQLite child finalize a recovered lost-stop root over loopback OTLP", async (t) => {
  const root = fixture(t),
    received = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const b of req) chunks.push(b);
    received.push(...spans(Buffer.concat(chunks)));
    res.setHeader("content-type", "application/x-protobuf");
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
  await new FileSecretStore(p.credentials).set("fixture", {
    api_key: "synthetic-api",
    secret_key: "synthetic-secret",
    identity_key: Buffer.alloc(32, 1).toString("hex"),
    content_key: Buffer.alloc(32, 2).toString("hex"),
  });
  const start = async () => {
    const runtime = new Runtime(
      p,
      path.resolve(__dirname, "../dist/src/runtime/storage-child.js"),
      "0.1.0",
      new AdapterRegistry(cursorAdapters(options(root))),
    );
    await runtime.start();
    return runtime;
  };
  let runtime = await start();
  t.after(() => runtime.stop());
  await runtimeRequest(p.socket, {
    operation: "observe-source",
    route_ref: route.id,
    source_schema: HOOK_SCHEMA,
    event: hook(),
  });
  await runtimeRequest(
    p.socket,
    { operation: "flush", deadlineMs: 3000 },
    3500,
  );
  assert.equal(received.length, 1);
  await runtime.stop();
  // Advance the durable deadline with the owner stopped, rather than sleeping 15 minutes
  // or adding a production clock/config bypass to the observer protocol.
  const db = new Database(path.join(p.state, "spool/spool.db"));
  db.prepare("UPDATE observed_turns SET deadline=?").run(Date.now() - 1);
  db.close();
  runtime = await start();
  await runtimeRequest(
    p.socket,
    { operation: "flush", deadlineMs: 3000 },
    3500,
  );
  assert.equal(received.length, 2);
  const [tool, parent] = received;
  assert.equal(parent.spanId, tool.parentSpanId);
  assert.equal(attr(parent, "fi.agent.outcome").stringValue, "incomplete");
  assert.equal(parent.endTimeUnixNano, tool.endTimeUnixNano);
  await runtime.stop();
});
