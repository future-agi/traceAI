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
