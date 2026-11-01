const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const base = path.resolve(__dirname, "../../../../../t");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const {
  Spool,
  OBSERVED_TURN_WINDOW_MS,
} = require("../dist/src/storage/store.js");
const { decodeTraces } = require("../dist/src/export/otlp.js");
const {
  normalizeCursorHook,
  HOOK_SCHEMA,
} = require("../dist/src/adapters/cursor/index.js");
const { hook } = require("../contract/cursor-fixtures.cjs");
const route = {
  id: "cursor.ide",
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
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(base, "cursor-lifecycle-"));
  const spool = new Spool(dir, "0.1.0");
  spool.route(route);
  t.after(() => {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return spool;
}
function admit(spool, event, epoch = 1) {
  const fact = normalizeCursorHook(event, "ide", {
    identityKey: Buffer.alloc(32, 1),
    policyRevision: "1",
    sourceSchema: HOOK_SCHEMA,
  })[0];
  return spool.admit(route.id, fact, undefined, true, epoch, true);
}
function take(spool) {
  const row = spool.next(route.id);
  assert.ok(row, "exportable row exists");
  const span = decodeTraces(row.bytes).resourceSpans[0].scopeSpans[0].spans[0];
  spool.complete(row.id, row.epoch, { state: "accepted", code: "ACCEPTED" });
  return span;
}
const outcome = (span) =>
  span.attributes.find((a) => a.key === "fi.agent.outcome")?.value.stringValue;

test("active turn across multiple fifteen-minute windows keeps root until real stop", (t) => {
  const spool = fixture(t);
  let clock = Date.now();
  t.mock.method(Date, "now", () => clock);
  admit(spool, hook());
  const first = take(spool);
  clock += OBSERVED_TURN_WINDOW_MS - 1000;
  admit(spool, { ...hook(), tool_use_id: "second-tool" });
  const second = take(spool);
  clock += OBSERVED_TURN_WINDOW_MS - 1000;
  admit(spool, hook("stop"));
  clock += 2000;
  const parent = take(spool);
  assert.equal(outcome(parent), "success");
  assert.equal(first.parentSpanId, parent.spanId);
  assert.equal(second.parentSpanId, parent.spanId);
  assert.equal(spool.next(route.id), undefined);
});

test("background drain never finalizes a live silent turn without a stop", (t) => {
  const spool = fixture(t);
  let clock = Date.now();
  t.mock.method(Date, "now", () => clock);
  admit(spool, hook());
  const first = take(spool);
  clock += OBSERVED_TURN_WINDOW_MS + 1;
  assert.equal(spool.next(route.id)?.work, undefined, "a background sweep cannot infer death from silence");
  admit(spool, { ...hook(), tool_use_id: "second-tool" });
  const second = take(spool);
  admit(spool, hook("stop"));
  clock += 2000;
  const parent = take(spool);
  assert.equal(outcome(parent), "success");
  assert.equal(first.parentSpanId, parent.spanId);
  assert.equal(second.parentSpanId, parent.spanId);
});

test("re-enable releases retained old-epoch root and does not resurrect stale capture", (t) => {
  const spool = fixture(t);
  admit(spool, hook());
  const child = take(spool);
  spool.disable(route.id);
  assert.equal(spool.next(route.id), undefined);
  spool.route({ ...route, epoch: 3 });
  assert.throws(() => admit(spool, hook("stop"), 1), /STALE_CAPTURE_EPOCH/);
  const parent = take(spool);
  assert.equal(parent.spanId, child.parentSpanId);
  assert.equal(outcome(parent), "incomplete");
  assert.equal(spool.next(route.id), undefined);
});

test("pending Cursor roots expire 24 hours after first observation and cannot be recovered", (t) => {
  const spool = fixture(t);
  let clock = Date.now();
  t.mock.method(Date, "now", () => clock);
  admit(spool, hook());
  const child = take(spool);
  assert.equal(spool.next(route.id), undefined);
  clock += 86_400_001;
  assert.equal(spool.expire(), 1);
  assert.equal(spool.next(route.id), undefined);
  assert.throws(() => admit(spool, { ...hook(), tool_use_id: "late-tool" }), /HELD_TURN_FINALIZED/);
  spool.disable(route.id);
  spool.route({ ...route, epoch: 3 });
  assert.equal(spool.next(route.id), undefined);
  assert.ok(child.parentSpanId.length, "the already exported child remains independently valid");
});

test("a stop just before the pending cap does not bypass its original expiry", (t) => {
  const spool = fixture(t);
  let clock = Date.now();
  t.mock.method(Date, "now", () => clock);
  admit(spool, hook());
  take(spool);
  clock += 86_400_000 - 1_000;
  admit(spool, hook("stop"));
  assert.equal(spool.next(route.id), undefined);
  clock += 2_000;
  assert.equal(spool.expire(), 1);
  assert.equal(spool.next(route.id), undefined);
});

test("preview retention documentation discloses the pending cap and unrecoverable root", () => {
  const readme = fs.readFileSync(path.resolve(__dirname, "../README.md"), "utf8");
  assert.match(readme, /pending roots.{0,120}24 hours/i);
  assert.match(readme, /expiry irreversibly drops their bytes/i);
  assert.match(readme, /cannot recover an expired root/i);
});

test("v3 pending Cursor root survives foundation schema upgrade and observed stop", (t) => {
  const Database = require("better-sqlite3");
  const dir = fs.mkdtempSync(path.join(base, "cursor-v3-upgrade-"));
  let spool = new Spool(dir, "0.1.0");
  t.after(() => {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  spool.route(route);
  admit(spool, hook());
  const child = take(spool);
  spool.close();
  const db = new Database(path.join(dir, "spool.db"));
  db.exec("DROP INDEX record_plans; DROP INDEX terminal_age");
  db.exec("ALTER TABLE records DROP COLUMN plan");
  db.exec("ALTER TABLE cursors DROP COLUMN updated");
  db.prepare("UPDATE meta SET value='3' WHERE key='schema'").run();
  db.pragma("user_version=3");
  db.close();
  spool = new Spool(dir, "0.1.1");
  let clock = Date.now();
  t.mock.method(Date, "now", () => clock);
  admit(spool, hook("stop"));
  clock += 2_000;
  const parent = take(spool);
  assert.equal(outcome(parent), "success");
  assert.equal(parent.spanId, child.parentSpanId);
});
