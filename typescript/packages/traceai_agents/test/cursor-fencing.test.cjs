const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { Runtime } = require("../dist/src/runtime/daemon.js");
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { FileSecretStore } = require("../dist/src/security/credentials.js");
const { Spool } = require("../dist/src/storage/store.js");
const {
  cursorAdapters,
  HOOK_SCHEMA,
  normalizeCursorHook,
} = require("../dist/src/adapters/cursor/index.js");
const { hook } = require("../contract/cursor-fixtures.cjs");

const binding = {
  id: "00000000-0000-4000-8000-000000000001",
  endpoint: "http://127.0.0.1:1/v1/traces",
  approvedOrigin: "http://127.0.0.1:1",
  project: "fixture",
  loopbackTest: true,
};
const route = (epoch = 1) => ({
  id: "cursor.ide",
  binding,
  policy: { revision: "1", categories: [], ceiling: [] },
  enabled: true,
  epoch,
});
const secret = { identity_key: Buffer.alloc(32, 1).toString("hex") };
const envelope = Buffer.from(
  JSON.stringify({
    protocol_version: 1,
    operation: "observe-source",
    route_ref: "cursor.ide",
    source_schema: HOOK_SCHEMA,
    event: hook(),
  }),
);
const socket = { setTimeout() {} };

// Exercise the asynchronous boundary with an empty throwaway credential directory
// and a mocked lookup. No credential file or real agent home is read.
function runtime(t, lookup, admit) {
  const base = path.resolve(__dirname, "../../../../../t");
  fs.mkdirSync(base, { recursive: true, mode: 0o700 });
  const root = fs.mkdtempSync(path.join(base, "lookup-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  t.mock.method(FileSecretStore.prototype, "get", lookup);
  const r = new Runtime(
    { credentials: root },
    "",
    "0.1.0",
    new AdapterRegistry(
      cursorAdapters({ node: process.execPath, hookEntry: "/unused-hook" }),
    ),
  );
  r.config = {
    schema_version: 1,
    profiles: { fixture: { storage: "file", credentialRef: "synthetic" } },
    routes: {
      "cursor.ide": {
        route: route(),
        profile: "fixture",
        agent: "cursor",
        surface: "ide",
        activation: {
          installed: true,
          trusted: "unknown",
          activated: false,
          restartRequired: true,
        },
      },
    },
  };
  r.storage = { call: admit };
  return r;
}

test("source events awaiting credentials cannot cross disable/re-enable or refresh", async (t) => {
  for (const mutation of [
    "disable",
    "reenable",
    "refresh",
    "auth-remove",
    "pause",
    "stop",
  ]) {
    await t.test(mutation, async (t) => {
      let resume;
      const r = runtime(
        t,
        () =>
          new Promise((resolve) => {
            resume = resolve;
          }),
        () => assert.fail("revoked source reached durable admission"),
      );
      const pending = r.dispatch(envelope, socket);
      const registered = r.config.routes["cursor.ide"];
      if (mutation === "disable") registered.route.enabled = false;
      if (mutation === "reenable") registered.route.epoch = 3;
      if (mutation === "refresh")
        r.config.routes["cursor.ide"] = structuredClone(registered);
      if (mutation === "auth-remove") r.config.profiles.fixture.removed = true;
      if (mutation === "pause") r.admissionPaused = true;
      if (mutation === "stop") r.stopped = true;
      resume(secret);
      await assert.rejects(pending, /STALE_CAPTURE_EPOCH/);
      assert.equal(r.sourceReceipts.size, 0);
    });
  }
});

test("a delayed durable ACK activates only its original capture epoch", async (t) => {
  let acknowledge, entered;
  const started = new Promise((resolve) => {
    entered = resolve;
  });
  const r = runtime(
    t,
    async () => secret,
    (op, args) => {
      assert.equal(op, "admit");
      assert.equal(args.expectedEpoch, 1);
      entered();
      return new Promise((resolve) => {
        acknowledge = resolve;
      });
    },
  );
  const pending = r.dispatch(envelope, socket);
  await started;
  r.config.routes["cursor.ide"].route.epoch = 3;
  acknowledge({ receipt_id: "1", duplicate: false });
  assert.equal((await pending).durable, true);
  assert.equal(r.sourceReceipts.get("cursor.ide"), 1);
  const status = await r.dispatch(
    Buffer.from(
      JSON.stringify({
        protocol_version: 1,
        operation: "status",
      }),
    ),
    socket,
  );
  assert.equal(status.agents[0].activation.activated, false);
});

test("an older ACK cannot erase a newer epoch's activation receipt", async (t) => {
  let acknowledge, entered;
  const started = new Promise((resolve) => {
    entered = resolve;
  });
  const r = runtime(
    t,
    async () => secret,
    (_op, args) => {
      if (args.expectedEpoch === 3)
        return Promise.resolve({ receipt_id: "2", duplicate: false });
      entered();
      return new Promise((resolve) => {
        acknowledge = resolve;
      });
    },
  );
  const pending = r.dispatch(envelope, socket);
  await started;
  r.config.routes["cursor.ide"].route.epoch = 3;
  await r.dispatch(envelope, socket);
  acknowledge({ receipt_id: "1", duplicate: false });
  await pending;
  assert.equal(r.sourceReceipts.get("cursor.ide"), 3);
});

test("source serialization is bounded independently of disconnected sockets and fences queued epochs", async (t) => {
  let release;
  const r = runtime(
    t,
    () =>
      new Promise((resolve) => {
        release = resolve;
      }),
    () => assert.fail("revoked queued event reached storage"),
  );
  const pending = Array.from({ length: 64 }, () =>
    r.dispatch(envelope, socket),
  );
  const settled = Promise.allSettled(pending);
  await assert.rejects(r.dispatch(envelope, socket), /SOURCE_QUEUE_FULL/);
  assert.equal(r.sourcePending, 64);
  assert.equal(r.sourceBytes, envelope.length * 64);
  r.config.routes["cursor.ide"].route.epoch = 3;
  release(secret);
  const results = await settled;
  assert.ok(
    results.every(
      (result) =>
        result.status === "rejected" &&
        result.reason.code === "STALE_CAPTURE_EPOCH",
    ),
  );
  assert.equal(r.sourcePending, 0);
  assert.equal(r.sourceBytes, 0);
  assert.equal(r.sourceQueues.size, 0);
});

test("storage rejects queued old-epoch observations before persisting facts or receipt clocks", (t) => {
  const base = path.resolve(__dirname, "../../../../../t");
  fs.mkdirSync(base, { recursive: true, mode: 0o700 });
  const root = fs.mkdtempSync(path.join(base, "epoch-"));
  const spool = new Spool(root, "0.1.0");
  t.after(() => {
    spool.close();
    fs.rmSync(root, { recursive: true, force: true });
  });
  spool.route(route());
  spool.disable("cursor.ide");
  spool.route(route(3));
  const f = normalizeCursorHook(hook(), "ide", {
    identityKey: Buffer.alloc(32, 1),
    policyRevision: "1",
    sourceSchema: HOOK_SCHEMA,
  })[0];
  assert.throws(
    () => spool.admit("cursor.ide", f, undefined, true, 1),
    /STALE_CAPTURE_EPOCH/,
  );
  assert.deepEqual(spool.status().counts, []);
  assert.equal(
    spool.admit("cursor.ide", f, undefined, true, 3).duplicate,
    false,
  );
  assert.equal(
    spool.admit("cursor.ide", f, undefined, true, 3).duplicate,
    true,
  );
});
