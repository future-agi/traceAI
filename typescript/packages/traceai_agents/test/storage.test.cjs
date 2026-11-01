const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Database = require("better-sqlite3");
const { Spool } = require("../dist/src/storage/store.js");
const { measure, STORAGE_LIMITS } = require("../dist/src/storage/budget.js");
const reference = require("../contract/reference.cjs");
const base = path.resolve(__dirname, "../../../../../storage-tests");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const key = Buffer.alloc(32, 1),
  context = {
    identityKey: key,
    policyRevision: "1",
    sourceSchema: "fixture-v1",
  };
function fact(id = "r", unit = "request") {
  return reference.normalize(
    {
      schema: "fixture-v1",
      session: "s",
      turn: "t",
      id,
      unit,
      prompt: "CANARY_SOURCE_TEXT",
    },
    context,
  )[0];
}
function route() {
  return {
    id: "reference.fixture",
    binding: {
      id: "00000000-0000-4000-8000-000000000001",
      endpoint: "http://127.0.0.1:1/v1/traces",
      approvedOrigin: "http://127.0.0.1:1",
      project: "fixture",
      loopbackTest: true,
      backendContract: "immutable-agent-v1",
    },
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  };
}
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(base, "spool-"));
  const spool = new Spool(dir, "0.1.0");
  spool.route(route());
  t.after(() => {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return { spool, dir };
}
test("FULL local durability, immutable replay, equal-value calls and usage uniqueness", (t) => {
  const { spool, dir } = fixture(t);
  const first = spool.admit(route().id, fact());
  assert.equal(first.duplicate, false);
  assert.equal(spool.admit(route().id, fact()).duplicate, true);
  spool.admit(route().id, fact("r2"));
  assert.equal(spool.status().counts[0].count, 2);
  assert.throws(
    () => spool.admit(route().id, { ...fact(), endNs: "3000000000" }),
    /HELD_IMMUTABLE_CONFLICT/,
  );
  const conflict = fact("r3");
  conflict.usage.factIds = fact().usage.factIds;
  assert.throws(() => spool.admit(route().id, conflict), /HELD_USAGE_CONFLICT/);
  const plan = fact("turn", "turn");
  plan.usage = { ...fact().usage, scope: "turn", contribution: "turn" };
  assert.throws(() => spool.admit(route().id, plan), /HELD_USAGE_PLAN/);
  for (const name of fs.readdirSync(dir))
    assert.ok(
      !fs
        .readFileSync(path.join(dir, name))
        .includes(Buffer.from("CANARY_SOURCE_TEXT")),
    );
});
test("profile isolation, destination immutability, policy fencing and bounded purge", (t) => {
  const { spool } = fixture(t);
  spool.admit(route().id, fact());
  const r = route();
  r.binding.endpoint = "http://127.0.0.1:2/v1/traces";
  r.binding.approvedOrigin = "http://127.0.0.1:2";
  assert.throws(() => spool.route(r), /HELD_DESTINATION_CHANGED/);
  const row = spool.next(route().id);
  spool.disable(route().id);
  assert.equal(spool.next(route().id), undefined);
  assert.throws(
    () =>
      spool.complete(row.id, row.epoch, {
        state: "accepted",
        code: "HTTP_ACCEPTED",
      }),
    /STALE_LEASE/,
  );
  assert.equal(spool.purge(route().id), 1);
  assert.equal(spool.status().logical_bytes, 0);
  const enabled = route();
  enabled.epoch = 2;
  assert.throws(() => spool.route(enabled), /HELD_POLICY/);
  enabled.epoch = 3;
  spool.route(enabled);
  assert.equal(spool.admit(route().id, fact()).duplicate, true);
  assert.throws(
    () =>
      spool.route({
        ...enabled,
        policy: {
          revision: "2",
          categories: ["prompts"],
          ceiling: ["prompts"],
        },
      }),
    /HELD_CONTENT_QUALIFICATION/,
  );
});
test("re-enable releases pre-disable queue under a new fenced epoch", (t) => {
  const { spool } = fixture(t);
  spool.admit(route().id, fact());
  const old = spool.next(route().id);
  spool.disable(route().id);
  assert.equal(spool.getRoute(route().id).epoch, 2);
  assert.equal(spool.next(route().id), undefined);
  const enabled = { ...route(), epoch: 3 };
  spool.route(enabled);
  const released = spool.next(route().id);
  assert.equal(released?.id, old.id);
  assert.equal(released?.epoch, 3);
  assert.equal(spool.status().held_disabled, 0);
  assert.throws(
    () => spool.complete(old.id, old.epoch, { state: "accepted", code: "HTTP_ACCEPTED" }),
    /STALE_LEASE/,
  );
  spool.complete(released.id, released.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
  assert.equal(spool.next(route().id), undefined);
  assert.equal(spool.status().logical_bytes, 0);
});
test("re-registration while disabled never releases captured work", (t) => {
  const { spool } = fixture(t);
  spool.admit(route().id, fact());
  spool.disable(route().id);
  const disabled = { ...route(), enabled: false, epoch: 2 };
  spool.route(disabled);
  assert.equal(spool.next(route().id), undefined);
  assert.equal(spool.status().held_disabled, 1);
});
test("accepted bytes are deleted but accounting receipts survive restart", (t) => {
  const dir = fs.mkdtempSync(path.join(base, "restart-"));
  let spool = new Spool(dir, "0.1.0");
  try {
    spool.route(route());
    spool.admit(route().id, fact());
    const row = spool.next(route().id);
    spool.complete(row.id, row.epoch, {
      state: "accepted",
      code: "HTTP_ACCEPTED",
    });
    assert.equal(spool.status().logical_bytes, 0);
    spool.close();
    spool = new Spool(dir, "0.1.0");
    assert.equal(spool.admit(route().id, fact()).duplicate, true);
  } finally {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
test("runtime upgrades deduplicate semantic replay and keep first wire bytes", (t) => {
  const dir = fs.mkdtempSync(path.join(base, "upgrade-"));
  let spool = new Spool(dir, "0.1.0");
  try {
    spool.route(route());
    spool.admit(route().id, fact());
    const original = spool.next(route().id);
    spool.close();
    spool = new Spool(dir, "0.1.1");
    assert.equal(spool.admit(route().id, fact()).duplicate, true);
    assert.deepEqual(spool.next(route().id).bytes, original.bytes);
    for (const changed of [
      { ...fact(), endNs: "3000000000" },
      { ...fact(), model: "different-model" },
      { ...fact(), usage: { ...fact().usage, input: 101, total: 121 } },
      { ...fact(), parentSpanId: "abcdef0123456789" },
    ])
      assert.throws(
        () => spool.admit(route().id, changed),
        /HELD_IMMUTABLE_CONFLICT/,
      );
    spool.complete(original.id, original.epoch, {
      state: "accepted",
      code: "HTTP_ACCEPTED",
    });
    spool.close();
    spool = new Spool(dir, "0.2.0");
    assert.equal(spool.admit(route().id, fact()).duplicate, true);
    assert.equal(
      spool.admit(route().id, fact("new-after-history")).duplicate,
      false,
    );
  } finally {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

for (const accepted of [false, true])
  test(`legacy ${accepted ? "accepted" : "pending"} wire receipts migrate only with proven equality`, () => {
    const dir = fs.mkdtempSync(path.join(base, "legacy-"));
    let spool = new Spool(dir, "0.1.0");
    try {
      spool.route(route());
      spool.admit(route().id, fact());
      const row = spool.next(route().id);
      if (accepted)
        spool.complete(row.id, row.epoch, {
          state: "accepted",
          code: "HTTP_ACCEPTED",
        });
      spool.close();
      const db = new Database(path.join(dir, "spool.db"));
      db.prepare("UPDATE records SET hash=?").run(
        require("node:crypto")
          .createHash("sha256")
          .update(row.bytes)
          .digest("hex"),
      );
      db.close();
      spool = new Spool(dir, "0.1.1");
      assert.throws(
        () => spool.admit(route().id, { ...fact(), outcome: "error" }),
        /HELD_IMMUTABLE_CONFLICT/,
      );
      assert.equal(spool.admit(route().id, fact()).duplicate, true);
      if (!accepted) assert.deepEqual(spool.next(route().id).bytes, row.bytes);
      spool.close();
      spool = new Spool(dir, "0.2.0");
      assert.equal(spool.admit(route().id, fact()).duplicate, true);
    } finally {
      spool.close();
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

test("unknown backend keeps ambiguous deliveries held", (t) => {
  const { spool } = fixture(t);
  const held = route();
  held.id = "unqualified.fixture";
  held.binding = {
    ...held.binding,
    id: "00000000-0000-4000-8000-000000000002",
  };
  delete held.binding.backendContract;
  spool.route(held);
  spool.admit(held.id, fact());
  const row = spool.next(held.id);
  spool.complete(row.id, row.epoch, {
    state: "ambiguous",
    code: "AMBIGUOUS_PARTIAL",
    nextAt: 0,
  });
  assert.equal(spool.next(held.id), undefined);
});

test("ambiguous rows retain immutable bytes and retry eligibility, but obey fences and expiry", (t) => {
  const { spool, dir } = fixture(t);
  spool.admit(route().id, fact());
  const row = spool.next(route().id);
  spool.complete(row.id, row.epoch, {
    state: "ambiguous",
    code: "AMBIGUOUS_RESPONSE",
    nextAt: Date.now() + 60000,
  });
  assert.equal(spool.next(route().id), undefined);
  spool.complete(row.id, row.epoch, {
    state: "ambiguous",
    code: "AMBIGUOUS_PARTIAL",
    nextAt: 0,
  });
  assert.equal(spool.next(route().id).state, "ambiguous");
  assert.deepEqual(spool.next(route().id).bytes, row.bytes);
  spool.complete(row.id, row.epoch, {
    state: "retry",
    code: "RETRY_HTTP",
    nextAt: 0,
  });
  assert.equal(spool.next(route().id).state, "ambiguous");
  const db = new Database(path.join(dir, "spool.db"));
  db.prepare("UPDATE records SET created=? WHERE id=?").run(
    Date.now() - 86400001,
    row.id,
  );
  db.close();
  assert.equal(spool.next(route().id), undefined);
  assert.equal(spool.expire(), 1);
  spool.admit(route().id, fact("fenced"));
  const pending = spool.next(route().id);
  spool.complete(pending.id, pending.epoch, {
    state: "ambiguous",
    code: "INVALID_RESPONSE",
    nextAt: 0,
  });
  spool.disable(route().id);
  assert.equal(spool.next(route().id), undefined);
});

test("terminal receipts and accounting indexes reclaim bounded pages after retention", (t) => {
  const { spool, dir } = fixture(t);
  for (let i = 0; i < 320; i++) {
    spool.admit(route().id, fact(`retained-${i}`));
    const row = spool.next(route().id);
    spool.complete(row.id, row.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
  }
  const db = new Database(path.join(dir, "spool.db"));
  db.prepare("UPDATE records SET created=?").run(Date.now() - 8 * 86400000);
  db.close();
  for (let i = 0; i < 5; i++) spool.expire();
  const check = new Database(path.join(dir, "spool.db"), { readonly: true });
  assert.equal(check.prepare("SELECT count(*) n FROM records").get().n, 0);
  assert.equal(check.prepare("SELECT count(*) n FROM usage").get().n, 0);
  assert.equal(check.prepare("SELECT count(*) n FROM plans").get().n, 0);
  const pagesAfterGc = check.pragma("page_count", { simple: true });
  check.close();
  for (let i = 0; i < 320; i++) {
    spool.admit(route().id, fact(`second-cycle-${i}`));
    const row = spool.next(route().id);
    spool.complete(row.id, row.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
  }
  const reused = new Database(path.join(dir, "spool.db"), { readonly: true });
  assert.ok(reused.pragma("page_count", { simple: true }) <= pagesAfterGc + 16);
  reused.close();
  assert.equal(spool.admit(route().id, fact("retained-0")).duplicate, false);
  assert.equal(spool.admit(route().id, fact("retained-0")).duplicate, true);
});

test("retention scans bounded candidates without killing the storage child", async (t) => {
  const { StorageClient } = require("../dist/src/runtime/storage-client.js");
  const dir = fs.mkdtempSync(path.join(base, "gc-candidates-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const initial = new Spool(dir, "0.1.0");
  initial.close();
  const db = new Database(path.join(dir, "spool.db"));
  const binding = route().binding.id;
  const insert = db.prepare("INSERT INTO records(route,binding,work,trace,span,hash,bytes,encrypted,epoch,state,created,plan) VALUES(?,?,?,?,?,?,NULL,0,1,'accepted',?,?)");
  const plan = db.prepare("INSERT INTO plans VALUES(?,?,?)");
  db.transaction(() => {
    for (let i = 0; i < 10000; i++) {
      const id = i.toString(16).padStart(64, "0");
      insert.run(route().id, binding, id, id.slice(32), id.slice(48), "semantic-v1:" + id, Date.now(), id);
      plan.run(binding, id, "request");
    }
    const id = "f".repeat(64);
    insert.run(route().id, binding, id, id.slice(32), id.slice(48), "semantic-v1:" + id, Date.now() - 8 * 86400000, id);
    plan.run(binding, id, "request");
  })();
  db.close();
  const client = new StorageClient(path.resolve(__dirname, "../dist/src/runtime/storage-child.js"));
  try {
    await client.call("init", { root: dir, version: "0.1.0" });
    assert.equal(await client.call("expire"), 0);
    assert.equal((await client.call("status")).state, "ready");
    // The orphan is beyond the first candidate window; subsequent bounded
    // passes must eventually reach and reclaim it without touching live plans.
    for (let i = 0; i < 80; i++) await client.call("expire");
  } finally {
    await client.close().catch(() => {});
  }
  const check = new Database(path.join(dir, "spool.db"), { readonly: true });
  assert.equal(check.prepare("SELECT count(*) n FROM plans").get().n, 10000);
  check.close();
});

test("new non-additive receipts cannot pin expired accounting plans", (t) => {
  const { spool, dir } = fixture(t);
  const source = (id, unit) => reference.normalize(
    { schema: "fixture-v1", session: "s", turn: id, id, unit }, context,
  )[0];
  spool.admit(route().id, source("ongoing-turn", "turn"));
  const current = spool.next(route().id);
  spool.complete(current.id, current.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
  for (let cycle = 0; cycle < 3; cycle++) {
    spool.admit(route().id, source("old-request-" + cycle, "request"));
    const old = spool.next(route().id);
    spool.complete(old.id, old.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
    const db = new Database(path.join(dir, "spool.db"));
    db.prepare("UPDATE records SET created=? WHERE id=?").run(Date.now() - 8 * 86400000, old.id);
    db.close();
    spool.expire();
    const check = new Database(path.join(dir, "spool.db"), { readonly: true });
    assert.equal(check.prepare("SELECT count(*) n FROM plans").get().n, 0);
    check.close();
  }
});

test("v1 receipts migrate without deleting pending work or its accounting plan", (t) => {
  const dir = fs.mkdtempSync(path.join(base, "migration-"));
  let spool = new Spool(dir, "0.1.0");
  try {
    spool.route(route());
    spool.admit(route().id, fact("legacy"));
    spool.close();
    const db = new Database(path.join(dir, "spool.db"));
    db.exec("DROP INDEX terminal_age; DROP INDEX record_plans; DROP INDEX legacy_plan_refs");
    db.exec("ALTER TABLE records DROP COLUMN legacy_unknown");
    db.exec("ALTER TABLE records DROP COLUMN plan");
    db.exec("ALTER TABLE cursors DROP COLUMN updated");
    db.prepare("UPDATE meta SET value='1' WHERE key='schema'").run();
    db.pragma("user_version=1");
    db.close();
    spool = new Spool(dir, "0.1.1");
    assert.equal(spool.admit(route().id, fact("legacy")).duplicate, true);
    spool.admit(route().id, fact("new"));
    const pending = spool.next(route().id);
    assert.equal(pending.work, fact("legacy").sourceWorkId);
    const db2 = new Database(path.join(dir, "spool.db"), { readonly: true });
    assert.equal(db2.pragma("user_version", { simple: true }), 4);
    assert.equal(db2.prepare("SELECT count(*) n FROM plans").get().n, 1);
    assert.equal(db2.prepare("SELECT legacy_unknown FROM records WHERE work=?").get(fact("legacy").sourceWorkId).legacy_unknown, 1);
    assert.equal(db2.prepare("SELECT legacy_unknown FROM records WHERE work=?").get(fact("new").sourceWorkId).legacy_unknown, 0);
    db2.close();
  } finally {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("v2 unknown-plan receipts pin old plans only until receipt expiry", (t) => {
  const dir = fs.mkdtempSync(path.join(base, "migration-v2-"));
  let spool = new Spool(dir, "0.1.0");
  const source = (id, unit) => reference.normalize(
    { schema: "fixture-v1", session: "s", turn: id, id, unit }, context,
  )[0];
  try {
    spool.route(route());
    for (const [id, unit] of [["old-request", "request"], ["legacy-turn", "turn"]]) {
      spool.admit(route().id, source(id, unit));
      const row = spool.next(route().id);
      spool.complete(row.id, row.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
    }
    spool.close();
    const old = new Database(path.join(dir, "spool.db"));
    old.exec("DROP INDEX legacy_plan_refs; ALTER TABLE records DROP COLUMN legacy_unknown");
    old.prepare("UPDATE meta SET value='2' WHERE key='schema'").run();
    old.pragma("user_version=2");
    old.close();
    spool = new Spool(dir, "0.1.1");
    const migrated = new Database(path.join(dir, "spool.db"), { readonly: true });
    assert.equal(migrated.pragma("user_version", { simple: true }), 3);
    assert.equal(migrated.prepare("SELECT legacy_unknown FROM records WHERE work=?").get(source("legacy-turn", "turn").sourceWorkId).legacy_unknown, 1);
    migrated.close();
    const age = new Database(path.join(dir, "spool.db"));
    age.prepare("UPDATE records SET created=? WHERE work=?").run(Date.now() - 8 * 86400000, source("old-request", "request").sourceWorkId);
    age.close();
    spool.expire();
    const pinned = new Database(path.join(dir, "spool.db"), { readonly: true });
    assert.equal(pinned.prepare("SELECT count(*) n FROM plans").get().n, 1);
    pinned.close();
    const ageAgain = new Database(path.join(dir, "spool.db"));
    ageAgain.prepare("UPDATE records SET created=? WHERE work=?").run(Date.now() - 8 * 86400000, source("legacy-turn", "turn").sourceWorkId);
    ageAgain.close();
    spool.expire();
    spool.admit(route().id, source("new-turn", "turn"));
    spool.expire();
    const check = new Database(path.join(dir, "spool.db"), { readonly: true });
    assert.equal(check.prepare("SELECT count(*) n FROM plans").get().n, 0);
    check.close();
    // The additive ledger and immutable receipt survive a schema
    // forward/backward/forward round trip, not only the first upgrade.
    spool.close();
    const rollback = new Database(path.join(dir, "spool.db"));
    rollback.exec("DROP INDEX legacy_plan_refs; ALTER TABLE records DROP COLUMN legacy_unknown");
    rollback.prepare("UPDATE meta SET value='2' WHERE key='schema'").run();
    rollback.pragma("user_version=2");
    rollback.close();
    spool = new Spool(dir, "0.1.2");
    assert.equal(spool.admit(route().id, source("new-turn", "turn")).duplicate, true);
    const forward = new Database(path.join(dir, "spool.db"), { readonly: true });
    assert.equal(forward.pragma("user_version", { simple: true }), 3);
    forward.close();
  } finally {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("active accounting and unexpired receipts survive receipt collection", (t) => {
  const { spool, dir } = fixture(t);
  spool.admit(route().id, fact("old"));
  spool.admit(route().id, fact("queued"));
  const old = spool.next(route().id);
  spool.complete(old.id, old.epoch, { state: "accepted", code: "HTTP_ACCEPTED" });
  const db = new Database(path.join(dir, "spool.db"));
  db.prepare("UPDATE records SET created=? WHERE id=?").run(Date.now() - 8 * 86400000, old.id);
  db.close();
  spool.expire();
  assert.equal(spool.admit(route().id, fact("queued")).duplicate, true);
  assert.equal(spool.next(route().id).work, fact("queued").sourceWorkId);
  assert.throws(() => spool.admit(route().id, { ...fact("queued"), endNs: "3000000000" }), /HELD_IMMUTABLE_CONFLICT/);
});

test("stalled second reader prevents another write and WAL stays within one transaction", (t) => {
  const { spool, dir } = fixture(t);
  spool.admit(route().id, fact("before"));
  const reader = new Database(path.join(dir, "spool.db"), { readonly: true });
  reader.exec("BEGIN");
  reader.prepare("SELECT count(*) FROM records").get();
  spool.admit(route().id, fact("first-uncheckpointed"));
  const before = measure(dir);
  assert.ok(before.wal <= STORAGE_LIMITS.wal);
  for (let i = 0; i < Number(process.env.FI_STORAGE_STRESS_ATTEMPTS ?? 30); i++)
    assert.throws(
      () => spool.admit(route().id, fact("blocked-" + i)),
      /PAUSED_CHECKPOINT/,
    );
  assert.equal(measure(dir).wal, before.wal);
  assert.ok(measure(dir).physical < STORAGE_LIMITS.cap);
  reader.exec("ROLLBACK");
  reader.close();
  spool.admit(route().id, fact("recovered"));
  assert.equal(measure(dir).wal, 0);
});
test("unexpected side files and symlinks stop admission before cursor movement", (t) => {
  const { spool, dir } = fixture(t);
  fs.writeFileSync(path.join(dir, "unexpected"), "safe", { mode: 0o600 });
  assert.throws(
    () => spool.admit(route().id, fact()),
    /PAUSED_UNEXPECTED_STATE/,
  );
  fs.unlinkSync(path.join(dir, "unexpected"));
  fs.symlinkSync("/nonexistent", path.join(dir, "unexpected"));
  assert.throws(
    () => spool.admit(route().id, fact()),
    /PAUSED_UNEXPECTED_STATE/,
  );
});

test("authentication holds persist without disabling capture or another binding", (t) => {
  const { spool } = fixture(t);
  spool.admit(route().id, fact());
  const row = spool.next(route().id);
  spool.complete(row.id, row.epoch, { state: "held", code: "HELD_AUTH" });
  assert.equal(spool.getRoute(route().id).enabled, true);
  spool.route(route()); // Startup re-registration must not fail or clear the hold.
  assert.equal(spool.next(route().id), undefined);
  const other = route();
  other.id = "other.fixture";
  other.binding.id = "00000000-0000-4000-8000-000000000002";
  spool.route(other);
  spool.admit(other.id, fact("other"));
  assert.ok(spool.next(other.id));
  assert.equal(spool.releaseAuth(route().id), 1);
  assert.equal(spool.next(route().id).id, row.id);
});
