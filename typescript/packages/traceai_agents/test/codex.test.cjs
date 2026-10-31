const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { performance } = require("node:perf_hooks");
const {
  CodexCodec,
  importCodex,
  CODEX_SCHEMA,
} = require("../dist/src/adapters/codex/codec.js");
const {
  RolloutReader,
  readImportSnapshot,
} = require("../dist/src/adapters/codex/reader.js");
const {
  preflightPlugin,
  activation,
} = require("../dist/src/adapters/codex/plugin.js");
const core = require("@traceai/agent-core");
const context = {
  identityKey: Buffer.alloc(32, 1),
  policyRevision: "1",
  sourceSchema: CODEX_SCHEMA,
};
const base = path.resolve(__dirname, "../../../../../codex-tests");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const at = (n) => new Date(1700000000000 + n).toISOString();
const line = (type, payload, n = 0) => ({ timestamp: at(n), type, payload });
const meta = (version = "0.166.0", extra = {}) =>
  line("session_meta", {
    id: "thread",
    session_id: "thread",
    cli_version: version,
    history_mode: "paginated",
    base_instructions: "PRIVACY_CANARY sk-canary123456",
    ...extra,
  });
const event = (type, p = {}, n = 1) =>
  line("event_msg", { type, turn_id: "turn", ...p }, n);
const usage = (response = "resp1", n = 3) =>
  line(
    "token_usage_record",
    {
      thread_id: "thread",
      session_id: "thread",
      turn_id: "turn",
      response_id: response,
      usage: {
        input_tokens: 10,
        output_tokens: 2,
        total_tokens: 12,
        cached_input_tokens: 3,
        cache_write_input_tokens: 0,
        reasoning_output_tokens: 1,
      },
      turn_token_usage: { total_tokens: 999 },
      thread_token_usage: { total_tokens: 999 },
    },
    n,
  );
const fixture = (version = "0.166.0") => [
  meta(version),
  event("task_started"),
  line(
    "turn_context",
    {
      turn_id: "turn",
      model: "gpt-5.4",
      cwd: "/private/canary",
      user_instructions: "PRIVACY_CANARY",
    },
    2,
  ),
  line(
    "response_item",
    {
      type: "message",
      id: "msg1",
      role: "assistant",
      content: [{ text: "PRIVACY_CANARY" }],
    },
    2,
  ),
  usage(),
  usage("resp2", 4),
  event(
    "token_count",
    {
      info: {
        last_token_usage: { total_tokens: 24 },
        total_token_usage: { total_tokens: 24 },
      },
    },
    4,
  ),
  line(
    "response_item",
    {
      type: "function_call",
      call_id: "call",
      name: "exec_command",
      arguments: "PRIVACY_CANARY",
      internal_chat_message_metadata_passthrough: { turn_id: "turn" },
    },
    5,
  ),
  line(
    "response_item",
    {
      type: "function_call_output",
      call_id: "call",
      output: "PRIVACY_CANARY",
      internal_chat_message_metadata_passthrough: { turn_id: "turn" },
    },
    6,
  ),
  event(
    "item_completed",
    {
      thread_id: "thread",
      item: { type: "ContextCompaction", id: "compact1" },
      started_at_ms: 1700000000006,
      completed_at_ms: 1700000000007,
    },
    7,
  ),
  event("task_complete", {}, 8),
];
const bytes = (rows) =>
  Buffer.from(rows.map((r) => JSON.stringify(r)).join("\n") + "\n");
function decode(rows) {
  return importCodex(bytes(rows), context);
}
for (const version of ["0.165.0", "0.166.0"])
  test(
    "pinned " +
      version +
      ": two equal requests, sibling tool, compaction, exact usage and privacy",
    () => {
      const f = decode(fixture(version));
      assert.equal(f.length, 5);
      const root = f.find((f) => f.unit === "turn"),
        requests = f.filter((f) => f.unit === "request");
      assert.equal(requests.length, 2);
      assert.notEqual(requests[0].sourceWorkId, requests[1].sourceWorkId);
      assert.equal(
        f.reduce((s, f) => s + (f.usage?.total ?? 0), 0),
        24,
      );
      for (const r of requests) {
        assert.equal(r.timing, "unknown");
        assert.equal(r.startNs, r.endNs);
        assert.equal(r.usage.cacheRead, 3);
        assert.equal(r.usage.reasoning, 1);
        assert.equal(r.usage.planId, core.planId(root.sourceWorkId));
      }
      for (const child of f.filter((f) => f.unit !== "turn"))
        assert.equal(child.parentSpanId, root.spanId);
      assert.equal(root.usage, undefined);
      assert.equal(f.find((f) => f.unit === "compaction").usage, undefined);
      assert.equal(f.find((f) => f.unit === "tool").outcome, "incomplete");
      assert.ok(!JSON.stringify(f).includes("PRIVACY_CANARY"));
      assert.ok(!JSON.stringify(f).includes("/private"));
      assert.deepEqual(decode(fixture(version)), f);
    },
  );
test("frozen source identities exclude parser policy and file coordinates", () => {
  const a = decode(fixture());
  const b = importCodex(bytes(fixture()), { ...context, policyRevision: "2" });
  assert.deepEqual(
    a.map((x) => x.sourceWorkId),
    b.map((x) => x.sourceWorkId),
  );
  const turn = core.workId(
    context.identityKey,
    "openai.codex/cli-conversation",
    "turn",
    ["thread"],
    ["thread", "turn"],
  );
  assert.equal(a[0].sourceWorkId, turn);
  assert.equal(
    a[1].sourceWorkId,
    core.workId(
      context.identityKey,
      "openai.codex/cli-conversation",
      "request",
      ["thread"],
      ["thread", "turn"],
      ["thread", "turn", "resp1"],
    ),
  );
});
test("anonymous messages, repeated usage summaries and hooks cannot fabricate requests", () => {
  const rows = fixture().filter((x) => x.type !== "token_usage_record");
  assert.equal(decode(rows).filter((f) => f.unit === "request").length, 0);
  const { codexAdapter } = require("../dist/src/adapters/codex/index.js");
  for (const hook_event_name of ["PreModelRequest", "PostModelRequest"])
    assert.deepEqual(
      codexAdapter("/unused").normalize(
        { hook_event_name, token_usage: { input_tokens: 100 } },
        context,
      ),
      [],
    );
});
function family(contained = false) {
  const parent = [
    meta(),
    event("task_started", {}, 1),
    event(
      "item_completed",
      {
        thread_id: "thread",
        started_at_ms: 1700000000002,
        completed_at_ms: 1700000000000 + (contained ? 8 : 3),
        item: {
          type: "CollabAgentToolCall",
          id: "spawn-call",
          tool: "spawn_agent",
          sender_thread_id: "thread",
          receiver_thread_ids: ["child"],
          status: "completed",
        },
      },
      contained ? 8 : 3,
    ),
    event("task_complete", {}, 9),
  ];
  const child = [
    meta("0.166.0", { id: "child", parent_thread_id: "thread" }),
    event("task_started", { turn_attribution: { parent_turn_id: "turn" } }, 3),
    {
      ...usage("child-response", 5),
      payload: { ...usage("child-response", 5).payload, thread_id: "child" },
    },
    event("task_complete", {}, 7),
  ];
  return { parent, child };
}
for (const contained of [false, true])
  test(
    "explicit spawn-call ancestry: " +
      (contained ? "foreground containment" : "detached link"),
    () => {
      const { parent, child } = family(contained);
      assert.throws(() => decode(child), /HELD_SPAWN_CONTEXT/);
      const facts = decode([...child, ...parent]); // ordering cannot substitute for causal evidence
      const root = facts.find(
        (f) => f.unit === "turn" && (f.links || f.parentSpanId),
      );
      const spawn = facts.find((f) => f.unit === "tool");
      assert.equal(root.sessionId, spawn.sessionId);
      const request = facts.find((f) => f.unit === "request");
      assert.equal(request.parentSpanId, root.spanId);
      assert.equal(request.traceId, root.traceId);
      if (contained) {
        assert.equal(root.parentSpanId, spawn.spanId);
        assert.equal(root.traceId, spawn.traceId);
        assert.equal(root.links, undefined);
      } else {
        assert.deepEqual(root.links, [
          { traceId: spawn.traceId, spanId: spawn.spanId },
        ]);
        assert.notEqual(root.traceId, spawn.traceId);
        assert.equal(root.parentSpanId, undefined);
      }
      assert.deepEqual(
        decode([...parent, ...child]).sort((a, b) =>
          a.sourceWorkId.localeCompare(b.sourceWorkId),
        ),
        facts.sort((a, b) => a.sourceWorkId.localeCompare(b.sourceWorkId)),
      );
    },
  );
test("cancelled and interrupted turns are outcomes, not fabricated model errors", () => {
  for (const reason of ["interrupted", "replaced"]) {
    const rows = fixture();
    rows[rows.length - 1] = event("turn_aborted", { reason }, 8);
    const f = decode(rows);
    assert.equal(
      f[0].outcome,
      reason === "interrupted" ? "interrupted" : "cancelled",
    );
    assert.equal(f.filter((f) => f.unit === "request").length, 2);
  }
});
test("unfinished import remains mutable until terminal source evidence arrives", () => {
  assert.deepEqual(decode(fixture().slice(0, -1)), []);
  const c = new CodexCodec(context);
  for (const r of fixture().slice(0, -1)) c.push(r);
  assert.deepEqual(c.facts(), []);
});
test("unknown schema, fork history, invalid usage and missing anchors fail closed", () => {
  for (const version of ["0.164.0", "0.167.0", "1.0.0"])
    assert.throws(() => decode(fixture(version)), /UNSUPPORTED_CODEX_VERSION/);
  const fork = fixture();
  fork[0] = meta("0.166.0", { forked_from_id: "old" });
  assert.throws(() => decode(fork), /HELD_HISTORY_MODE/);
  const broken = fixture();
  broken[4].payload.usage.total_tokens = 123;
  assert.throws(() => decode(broken), /UNRESOLVED_USAGE/);
  const missing = fixture();
  delete missing[4].payload.response_id;
  assert.throws(() => decode(missing), /HELD_IDENTITY/);
  assert.throws(
    () => importCodex(bytes(fixture()).subarray(0, -1), context),
    /PARTIAL_SOURCE_TAIL/,
  );
});
test("repeat same immutable request is one owner; conflicting corrections hold", () => {
  const rows = fixture();
  rows.splice(5, 0, usage());
  assert.equal(decode(rows).filter((x) => x.unit === "request").length, 2);
  rows[5].payload.usage = {
    input_tokens: 11,
    output_tokens: 2,
    total_tokens: 13,
  };
  assert.throws(() => decode(rows), /HELD_SOURCE_CONFLICT/);
});
test("native 0644 import, incremental partial tails, symlink and truncation fencing", () => {
  const root = fs.mkdtempSync(path.join(base, "reader-"));
  const sessions = path.join(root, "sessions"),
    dir = path.join(sessions, "2026/10/31");
  fs.mkdirSync(dir, { recursive: true, mode: 0o755 });
  const file = path.join(dir, "rollout-fixture.jsonl"),
    raw = bytes(fixture());
  fs.writeFileSync(file, raw.subarray(0, raw.length - 8), { mode: 0o644 });
  const c = new CodexCodec(context, "thread"),
    reader = new RolloutReader(sessions, file, c);
  assert.equal(reader.read().caughtUp, false);
  assert.deepEqual(c.facts(), []);
  fs.appendFileSync(file, raw.subarray(raw.length - 8));
  assert.equal(reader.read().caughtUp, true);
  assert.deepEqual(c.facts(), decode(fixture()));
  assert.deepEqual(readImportSnapshot(file), raw);
  const symlink = path.join(dir, "rollout-symlink.jsonl");
  fs.symlinkSync(file, symlink);
  assert.throws(() => readImportSnapshot(symlink));
  fs.truncateSync(file, 1);
  assert.throws(() => reader.read(), /HELD_ROTATED_SOURCE/);
  assert.throws(
    () => new RolloutReader(sessions, path.join(root, "outside.jsonl"), c),
    /UNSAFE_SOURCE_PATH/,
  );
});
test("installer is idempotent, immutable, consent-gated and does not edit host configuration or trust", async () => {
  const root = fs.mkdtempSync(path.join(base, "plugin-")),
    home = path.join(root, "codex");
  fs.mkdirSync(home, { mode: 0o700 });
  const cfg = path.join(home, "config.toml");
  fs.writeFileSync(cfg, 'model="unchanged"\n# PRIVACY_CANARY', { mode: 0o600 });
  const before = fs.readFileSync(cfg);
  const old = process.env.CODEX_HOME;
  process.env.CODEX_HOME = home;
  const c = {
    routeRef: "codex.cli",
    socketPath: path.join(root, "runtime.sock"),
    surface: "cli",
    profile: "fixture",
    binding: {},
    transcriptConsent: true,
  };
  const entry = path.resolve(__dirname, "../bin/fi-agent-codex-hook.cjs");
  try {
    assert.throws(
      () =>
        preflightPlugin(
          "enable",
          { ...c, transcriptConsent: false },
          entry,
          "0.1.0",
        ),
      /TRANSCRIPT_CONSENT_REQUIRED/,
    );
    const a = preflightPlugin("enable", c, entry, "0.1.0");
    await a.commit();
    assert.equal(activation(c.socketPath).activated, false);
    const registration = JSON.parse(
      fs.readFileSync(path.join(root, "codex/registration.json")),
    );
    assert.ok(
      fs.existsSync(path.join(registration.bundle, "codex/bin/observer.cjs")),
    );
    const hooks = JSON.parse(
      fs.readFileSync(path.join(registration.bundle, "codex/hooks/hooks.json")),
    );
    assert.ok(
      hooks.hooks.PreModelRequest[0].hooks[0].command.includes(
        "${PLUGIN_ROOT}",
      ),
    );
    assert.ok(!hooks.hooks.PreModelRequest[0].hooks[0].command.includes("npx"));
    fs.writeFileSync(
      path.join(root, "codex/activation.json"),
      JSON.stringify({
        activated: true,
        contract: 1,
        fingerprint: registration.fingerprint,
      }),
      { mode: 0o600 },
    );
    await preflightPlugin("enable", c, entry, "0.1.0").commit();
    assert.equal(activation(c.socketPath).activated, true);
    await preflightPlugin("update", c, entry, "0.1.0").commit();
    assert.equal(activation(c.socketPath).activated, true);
    assert.deepEqual(fs.readFileSync(cfg), before);
    await preflightPlugin(
      "disable",
      { ...c, transcriptConsent: false },
      entry,
      "0.1.0",
    ).commit();
    assert.equal(activation(c.socketPath).installed, false);
    assert.deepEqual(fs.readFileSync(cfg), before);
  } finally {
    if (old === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = old;
  }
});
test("hook failures stay neutral with no stdout, stderr or credential dependency", async () => {
  const entry = path.resolve(__dirname, "../bin/fi-agent-codex-hook.cjs");
  const durations = [];
  for (const input of [
    "not json",
    JSON.stringify({
      hook_event_name: "PostModelRequest",
      session_id: "thread",
      transcript_path: null,
      error: "PRIVACY_CANARY",
      token_usage: { total_tokens: 99 },
    }),
  ]) {
    const start = performance.now();
    const result = await new Promise((resolve, reject) => {
      const p = spawn(
        process.execPath,
        [
          entry,
          "--socket",
          "/nonexistent-fixture.sock",
          "--route",
          "codex.cli",
        ],
        { env: { PATH: "/usr/bin:/bin" }, stdio: ["pipe", "pipe", "pipe"] },
      );
      let out = "",
        err = "";
      p.stdout.on("data", (b) => (out += b));
      p.stderr.on("data", (b) => (err += b));
      p.on("error", reject);
      p.on("exit", (code) => resolve({ code, out, err }));
      p.stdin.end(input);
    });
    durations.push(performance.now() - start);
    assert.deepEqual(result, { code: 0, out: "", err: "" });
  }
  assert.ok(durations.every((ms) => ms < 1000)); // safety bound, not a p95 certification
});

test("upgrade stages a new trusted-by-host bundle, fences old callbacks and rolls back owned state", async () => {
  const root = fs.mkdtempSync(path.join(base, "upgrade-"));
  const c = {
    routeRef: "codex.cli",
    socketPath: path.join(root, "runtime.sock"),
    surface: "cli",
    profile: "fixture",
    binding: {},
    transcriptConsent: true,
  };
  const entry = path.resolve(__dirname, "../bin/fi-agent-codex-hook.cjs");
  const oldHome = process.env.CODEX_HOME;
  process.env.CODEX_HOME = path.join(root, "home");
  const file = path.join(root, "codex/registration.json");
  const { registration } = require("../dist/src/adapters/codex/plugin.js");
  const { CodexObserver } = require("../dist/src/adapters/codex/observer.js");
  await preflightPlugin("enable", c, entry, "0.1.0").commit();
  const before = fs.readFileSync(file),
    old = registration(c.socketPath);
  fs.writeFileSync(
    path.join(root, "codex/activation.json"),
    JSON.stringify({
      contract: 1,
      activated: true,
      fingerprint: old.fingerprint,
    }),
    { mode: 0o600 },
  );
  const candidate = preflightPlugin("update", c, entry, "0.1.1");
  assert.deepEqual(candidate.preview, {
    replacement: true,
    trustRequired: true,
  });
  assert.deepEqual(fs.readFileSync(file), before); // preview has no writes
  await candidate.commit();
  const next = registration(c.socketPath);
  assert.notEqual(next.bundle, old.bundle);
  assert.equal(next.previousBundle, old.bundle);
  assert.equal(next.enabled, true);
  assert.equal(activation(c.socketPath).activated, false);
  const observer = new CodexObserver(c, {
    normalize: context,
    admit: async () => {},
  });
  try {
    assert.throws(
      () =>
        observer.accept({
          event: "SessionStart",
          session: "thread",
          path: null,
          bundle: old.fingerprint,
        }),
      /INVALID_OBSERVER_EVENT/,
    );
    assert.equal(activation(c.socketPath).activated, false);
    observer.accept({
      event: "SessionStart",
      session: "thread",
      path: null,
      bundle: next.fingerprint,
    });
    assert.equal(activation(c.socketPath).activated, true);
  } finally {
    await observer.stop();
  }
  await candidate.rollback();
  assert.deepEqual(fs.readFileSync(file), before);
  assert.equal(activation(c.socketPath).activated, true);
  await preflightPlugin("disable", c, entry, "0.1.0").commit();
  await preflightPlugin(
    "update",
    { ...c, transcriptConsent: false },
    entry,
    "0.1.1",
  ).commit();
  assert.equal(registration(c.socketPath).enabled, false);
  await preflightPlugin(
    "update",
    { ...c, transcriptConsent: false, rollback: true },
    entry,
    "0.1.1",
  ).commit();
  assert.equal(registration(c.socketPath).bundle, old.bundle);
  assert.equal(registration(c.socketPath).enabled, false);
  if (oldHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = oldHome;
  fs.rmSync(root, { recursive: true, force: true });
});

test("observer replays upgraded receipts and isolates old immutable conflicts from new turns", async () => {
  const { Spool } = require("../dist/src/storage/store.js");
  const { CodexObserver } = require("../dist/src/adapters/codex/observer.js");
  const { registration } = require("../dist/src/adapters/codex/plugin.js");
  const root = fs.mkdtempSync(path.join(base, "replay-"));
  const home = path.join(root, "agent"),
    dir = path.join(home, "sessions/2026/10/31");
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const file = path.join(dir, "rollout-fixture.jsonl");
  const previous = process.env.CODEX_HOME;
  process.env.CODEX_HOME = home;
  const c = {
    routeRef: "codex.cli",
    socketPath: path.join(root, "runtime.sock"),
    surface: "cli",
    profile: "fixture",
    transcriptConsent: true,
    binding: {
      id: "00000000-0000-4000-8000-000000000001",
      endpoint: "http://127.0.0.1:1/traces",
      approvedOrigin: "http://127.0.0.1:1",
      project: "fixture",
      loopbackTest: true,
    },
  };
  let spool, observer;
  try {
    await preflightPlugin(
      "enable",
      c,
      path.resolve(__dirname, "../bin/fi-agent-codex-hook.cjs"),
      "0.1.0",
    ).commit();
    spool = new Spool(path.join(root, "spool"), "0.1.0");
    spool.route({
      id: c.routeRef,
      binding: c.binding,
      enabled: true,
      epoch: 1,
      policy: core.STRUCTURE_POLICY,
    });
    assert.deepEqual(decode(fixture().slice(0, -1)), []); // importing an active prefix cannot poison its root
    const oldFacts = decode(fixture());
    for (const fact of oldFacts) spool.admit(c.routeRef, fact);
    spool.close();
    spool = new Spool(path.join(root, "spool"), "0.1.1");
    const later = [
      event("task_started", { turn_id: "later" }, 10),
      event("task_complete", { turn_id: "later" }, 11),
    ];
    fs.writeFileSync(file, bytes([...fixture(), ...later]), { mode: 0o600 });
    observer = new CodexObserver(c, {
      normalize: context,
      admit: async (fact) => spool.admit(c.routeRef, fact),
    });
    const callback = {
      event: "Stop",
      session: "thread",
      path: file,
      bundle: registration(c.socketPath).fingerprint,
    };
    observer.accept(callback);
    await observer.tick();
    for (const active of observer.active.values()) active.quietSince -= 2100;
    await observer.tick();
    assert.equal(observer.status().admitted, oldFacts.length + 1);
    assert.deepEqual(observer.status().reasons, []);
    await observer.stop();
    const changed = fixture();
    changed[changed.length - 1] = event(
      "turn_aborted",
      { reason: "interrupted" },
      8,
    );
    fs.writeFileSync(
      file,
      bytes([
        ...changed,
        ...later,
        event("task_started", { turn_id: "newest" }, 12),
        event("task_complete", { turn_id: "newest" }, 13),
      ]),
    );
    observer = new CodexObserver(c, {
      normalize: context,
      admit: async (fact) => spool.admit(c.routeRef, fact),
    });
    observer.accept(callback);
    await observer.tick();
    for (const active of observer.active.values()) active.quietSince -= 2100;
    await observer.tick();
    assert.ok(observer.status().reasons.includes("HELD_IMMUTABLE_CONFLICT"));
    assert.equal(observer.status().admitted, oldFacts.length + 1); // all except the conflicting root
    assert.equal(spool.status().counts[0].count, oldFacts.length + 2);
  } finally {
    await observer?.stop();
    spool?.close();
    if (previous === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = previous;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("bounded explicit directory import preserves spawn context and refuses symlink members", () => {
  const dir = fs.mkdtempSync(path.join(base, "family-"));
  const { parent, child } = family();
  fs.writeFileSync(path.join(dir, "rollout-parent.jsonl"), bytes(parent), {
    mode: 0o600,
  });
  fs.writeFileSync(path.join(dir, "rollout-child.jsonl"), bytes(child), {
    mode: 0o600,
  });
  assert.equal(importCodex(readImportSnapshot(dir), context).length, 4);
  fs.symlinkSync(
    path.join(dir, "rollout-parent.jsonl"),
    path.join(dir, "rollout-link.jsonl"),
  );
  assert.throws(() => readImportSnapshot(dir));
  fs.rmSync(dir, { recursive: true, force: true });
});
