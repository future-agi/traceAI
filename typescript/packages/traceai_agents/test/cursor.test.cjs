const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const { spawn, spawnSync } = require("node:child_process");
const { parse } = require("jsonc-parser");
const { validateFact } = require("@traceai/agent-core");
const { Spool } = require("../dist/src/storage/store.js");
const { ImportPlanner } = require("../dist/src/import.js");
const {
  cursorAdapters,
  HOOK_SCHEMA,
  STREAM_SCHEMA,
  minimizeCursorEvent,
  normalizeCursorHook,
  readCursorStream,
} = require("../dist/src/adapters/cursor/index.js");
const {
  cursorChange,
  cursorProbe,
} = require("../dist/src/adapters/cursor/install.js");
const { decodeTraces } = require("../dist/src/export/otlp.js");
const { runtimeRequest } = require("../dist/src/runtime/daemon.js");
const {
  hook,
  stream,
  bytes,
  canary,
} = require("../contract/cursor-fixtures.cjs");
const base = path.resolve(__dirname, "../../../../../t");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const bin = path.resolve(__dirname, "../bin/fi-agent.cjs");
const hookBin = path.resolve(__dirname, "../bin/fi-cursor-hook.cjs");
const context = {
  identityKey: Buffer.alloc(32, 1),
  policyRevision: "1",
  sourceSchema: HOOK_SCHEMA,
};
const binding = {
  id: "00000000-0000-4000-8000-000000000001",
  endpoint: "http://127.0.0.1:1/v1/traces",
  approvedOrigin: "http://127.0.0.1:1",
  project: "fixture",
  loopbackTest: true,
};
function fixture(t) {
  const root = fs.mkdtempSync(path.join(base, "c-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}
function installOptions(root) {
  return {
    node: process.execPath,
    hookEntry: hookBin,
    hooksFile: path.join(root, ".cursor/hooks.json"),
  };
}
function installContext(root, surface = "ide") {
  return {
    routeRef: "cursor." + surface,
    socketPath: path.join(root, "s/runtime.sock"),
    surface,
    profile: "fixture",
    binding,
    transcriptConsent: false,
    preview: true,
    captureMode: "hooks",
  };
}
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
function hookProcess(
  root,
  input,
  socket = path.join(root, "s/runtime.sock"),
  event = input.hook_event_name,
) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        hookBin,
        "--surface",
        "ide",
        "--event",
        event,
        "--route",
        "cursor.ide",
        "--socket",
        socket,
      ],
      {
        env: { PATH: "/usr/bin:/bin", HOME: root },
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (b) => (stdout += b));
    child.stderr.on("data", (b) => (stderr += b));
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
    child.stdin.on("error", () => {});
    child.stdin.end(typeof input === "string" ? input : JSON.stringify(input));
  });
}

test("surface/version-qualified observer minimizes before IPC and never fabricates LLM work", () => {
  const event = minimizeCursorEvent(hook(), "ide");
  assert.ok(!JSON.stringify(event).includes(canary));
  const [tool] = normalizeCursorHook(event, "ide", context);
  const [turn] = normalizeCursorHook(hook("stop"), "ide", context);
  for (const f of [tool, turn]) validateFact(f);
  assert.equal(tool.unit, "tool");
  assert.equal(turn.unit, "turn");
  assert.equal(tool.parentSpanId, turn.spanId);
  assert.equal(tool.traceId, turn.traceId);
  assert.equal(tool.endNs, "40000000");
  assert.equal(tool.timing, "estimated");
  assert.equal(turn.timing, "unknown");
  assert.equal(turn.startNs, turn.endNs);
  assert.equal(tool.usage, undefined);
  assert.equal(turn.usage, undefined);
  for (const event of [
    "afterAgentThought",
    "afterAgentResponse",
    "preToolUse",
    "beforeSubmitPrompt",
    "subagentStop",
  ])
    assert.throws(
      () => minimizeCursorEvent(hook(event), "ide"),
      /UNSUPPORTED_CURSOR_EVENT/,
    );
  assert.throws(
    () => minimizeCursorEvent({ ...hook(), cursor_version: "99.0" }, "ide"),
    /UNKNOWN_CURSOR_VERSION/,
  );
  assert.throws(
    () => minimizeCursorEvent(hook(), "cli"),
    /UNKNOWN_CURSOR_VERSION/,
  );
  assert.throws(
    () => minimizeCursorEvent({ ...hook(), tool_use_id: "" }, "ide"),
    /HELD_IDENTITY/,
  );
  const changed = normalizeCursorHook(
    { ...hook(), text: "different", model: "different" },
    "ide",
    { ...context, policyRevision: "2" },
  )[0];
  assert.equal(changed.sourceWorkId, tool.sourceWorkId);
  const second = normalizeCursorHook(
    { ...hook(), tool_use_id: "tool-2" },
    "ide",
    context,
  )[0];
  assert.notEqual(second.sourceWorkId, tool.sourceWorkId);
  assert.equal(
    minimizeCursorEvent({ ...hook(), tool_name: canary }, "ide").tool_name,
    "custom",
  );
});

test("tool failure, permission denial, cancellation and terminal outcomes stay distinct", () => {
  const normalize = (e) => normalizeCursorHook(e, "ide", context)[0];
  assert.equal(
    normalize({
      ...hook("postToolUseFailure"),
      failure_type: "permission_denied",
    }).outcome,
    "permission_denied",
  );
  assert.equal(
    normalize({ ...hook("postToolUseFailure"), is_interrupt: true }).outcome,
    "cancelled",
  );
  assert.equal(
    normalize({ ...hook("postToolUseFailure"), failure_type: "timeout" })
      .outcome,
    "error",
  );
  assert.equal(
    normalize({ ...hook("stop"), status: "aborted" }).outcome,
    "cancelled",
  );
  assert.equal(
    normalize({ ...hook("stop"), status: "error" }).outcome,
    "error",
  );
});

test("receipt clocks and sanitized facts commit together; delayed replay stays immutable", (t) => {
  const root = fixture(t),
    dir = path.join(root, "spool");
  const spool = new Spool(dir, "0.1.0");
  t.after(() => spool.close());
  spool.route({
    id: "cursor.ide",
    binding,
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  });
  const f = normalizeCursorHook(hook(), "ide", context)[0];
  const first = spool.admit("cursor.ide", f, undefined, true);
  assert.equal(first.duplicate, false);
  assert.deepEqual(spool.admit("cursor.ide", f, undefined, true), {
    receipt_id: first.receipt_id,
    duplicate: true,
  });
  assert.throws(
    () =>
      spool.admit("cursor.ide", { ...f, outcome: "error" }, undefined, true),
    /HELD_IMMUTABLE_CONFLICT/,
  );
  assert.throws(
    () =>
      spool.admit(
        "cursor.ide",
        { ...f, timing: "observed_request" },
        undefined,
        true,
      ),
    /INVALID_OBSERVATION_CLOCK/,
  );
  assert.equal(spool.status().counts[0].count, 1);
  for (const file of fs.readdirSync(dir))
    assert.ok(
      !fs.readFileSync(path.join(dir, file)).includes(Buffer.from(canary)),
    );
});

test("documented model_call_id flushes reconstruct two units, not deltas, thoughts, or final answers", () => {
  const c = { ...context, sourceSchema: STREAM_SCHEMA };
  const source = stream();
  source.splice(4, 0, source[3]); // repeated buffered flush, not another request
  source.splice(5, 0, {
    type: "thinking",
    session_id: source[0].session_id,
    text: canary,
  });
  const facts = readCursorStream(bytes(source), c);
  assert.equal(facts.length, 5);
  assert.equal(facts.filter((f) => f.unit === "request").length, 2);
  assert.equal(facts.filter((f) => f.unit === "tool").length, 2);
  for (const f of facts) {
    validateFact(f);
    assert.equal(f.usage, undefined);
    assert.equal(f.content, undefined);
  }
  for (const f of facts.slice(1)) assert.equal(f.parentSpanId, facts[0].spanId);
  assert.ok(
    facts
      .filter((f) => f.unit === "request")
      .every(
        (f) => f.timing === "message_interval" && f.outcome === "incomplete",
      ),
  );
  assert.ok(facts.filter((f) => f.unit === "tool").every((f) => !f.causedBy)); // no FIFO causal guess
  assert.ok(!JSON.stringify(facts).includes(canary));
  assert.deepEqual(readCursorStream(bytes(source), c), facts);
});

test("unknown persisted transcripts, partial tails, untimestamped streams, and missing turn IDs hold", () => {
  const c = { ...context, sourceSchema: STREAM_SCHEMA };
  assert.throws(
    () => readCursorStream(bytes(stream()).subarray(0, -1), c),
    /PARTIAL_SOURCE_TAIL/,
  );
  const missing = stream();
  delete missing.at(-1).request_id;
  assert.throws(() => readCursorStream(bytes(missing), c), /HELD_IDENTITY/);
  const untimed = stream().map(({ timestamp_ms, ...r }) => r);
  assert.throws(() => readCursorStream(bytes(untimed), c), /HELD_SOURCE_TIME/);
  assert.throws(
    () =>
      readCursorStream(bytes(stream()), { ...c, sourceSchema: "new-schema" }),
    /UNKNOWN_SOURCE_SCHEMA/,
  );
  assert.throws(
    () => readCursorStream(Buffer.alloc(1048577), c),
    /SOURCE_TOO_LARGE/,
  );
  assert.throws(
    () => readCursorStream(bytes([{ role: "assistant", text: canary }]), c),
    /INCOMPLETE_CURSOR_STREAM/,
  );
  const conflict = stream();
  conflict.splice(4, 0, { ...conflict[3], timestamp_ms: 1793400000150 });
  assert.throws(
    () => readCursorStream(bytes(conflict), c),
    /HELD_IMMUTABLE_CONFLICT/,
  );
});

test("explicit protected import plans contain no content and reject symlinks/source changes", async (t) => {
  const root = fixture(t),
    source = path.join(root, "stream.jsonl");
  fs.writeFileSync(source, bytes(stream()), { mode: 0o600 });
  const adapter = cursorAdapters(installOptions(root))[1];
  const planner = new ImportPlanner(path.join(root, "plans"));
  const c = { ...context, sourceSchema: STREAM_SCHEMA };
  const plan = planner.plan(adapter, source, c);
  assert.equal(plan.workIds.length, 5);
  assert.ok(!JSON.stringify(plan).includes(canary));
  const facts = [];
  assert.deepEqual(
    await planner.commit(plan.id, adapter, source, c, async (f) =>
      facts.push(f),
    ),
    { admitted: 5, pending: 0 },
  );
  fs.symlinkSync(source, path.join(root, "linked.jsonl"));
  assert.throws(() =>
    planner.plan(adapter, path.join(root, "linked.jsonl"), c),
  );
  fs.appendFileSync(source, "\n");
  await assert.rejects(
    planner.commit(plan.id, adapter, source, c, async () => {}),
    /SOURCE_CHANGED/,
  );
});

test("owned observer install/disable preserve unrelated config, comments, and the other surface", async (t) => {
  const root = fixture(t),
    o = installOptions(root),
    c = installContext(root);
  fs.mkdirSync(path.dirname(o.hooksFile), { mode: 0o700 });
  fs.writeFileSync(
    o.hooksFile,
    '{\n // preserve this comment\n "version": 1, "unrelated": "private-setting", "hooks": {"sessionStart":[{"command":"keep-me"}]}\n}',
    { mode: 0o600 },
  );
  await (await cursorChange("enable", "ide", c, o)).commit();
  assert.equal((await cursorProbe("ide", c, o)).installed, true);
  assert.equal((await cursorProbe("ide", c, o)).activated, false);
  const text = fs.readFileSync(o.hooksFile, "utf8");
  assert.match(text, /preserve this comment/);
  assert.match(text, /keep-me/);
  assert.ok(!text.includes(canary));
  assert.ok(!text.includes("preToolUse"));
  await (await cursorChange("enable", "ide", c, o)).commit();
  assert.equal(fs.readFileSync(o.hooksFile, "utf8"), text);
  await (
    await cursorChange("enable", "cli", installContext(root, "cli"), o)
  ).commit();
  await (await cursorChange("disable", "ide", c, o)).commit();
  const d = parse(fs.readFileSync(o.hooksFile, "utf8"));
  assert.equal(d.hooks.stop.length, 1);
  assert.match(d.hooks.stop[0].command, /cursor.cli/);
  assert.equal(d.unrelated, "private-setting");
  assert.deepEqual(d.hooks.sessionStart, [{ command: "keep-me" }]);
});

test("installation refuses preview omission, consented unknown schemas, unsafe paths, and concurrent edits", async (t) => {
  const root = fixture(t),
    o = installOptions(root),
    c = installContext(root);
  await assert.rejects(
    cursorChange("enable", "ide", { ...c, preview: false }, o),
    /CURSOR_PREVIEW_REQUIRED/,
  );
  await assert.rejects(
    cursorChange("enable", "ide", { ...c, transcriptConsent: true }, o),
    /HELD_CURSOR_TRANSCRIPT_SCHEMA/,
  );
  const change = await cursorChange("enable", "ide", c, o);
  fs.mkdirSync(path.dirname(o.hooksFile), { mode: 0o700 });
  fs.writeFileSync(o.hooksFile, '{"version":1,"hooks":{},"concurrent":true}', {
    mode: 0o600,
  });
  await assert.rejects(change.commit(), /CURSOR_CONFIG_CONFLICT/);
  assert.match(fs.readFileSync(o.hooksFile, "utf8"), /concurrent/);
  fs.renameSync(o.hooksFile, path.join(root, "host.json"));
  fs.symlinkSync(path.join(root, "host.json"), o.hooksFile);
  await assert.rejects(cursorChange("enable", "ide", c, o));
});

test("duplicate config keys, modified owned hooks, and legacy emitters fail closed", async (t) => {
  const root = fixture(t),
    o = installOptions(root),
    c = installContext(root);
  fs.mkdirSync(path.dirname(o.hooksFile), { mode: 0o700 });
  fs.writeFileSync(o.hooksFile, '{"version":1,"hooks":{},"hooks":{}}', {
    mode: 0o600,
  });
  await assert.rejects(
    cursorChange("enable", "ide", c, o),
    /CURSOR_CONFIG_CONFLICT/,
  );
  fs.writeFileSync(
    o.hooksFile,
    JSON.stringify({
      version: 1,
      hooks: {
        afterAgentResponse: [{ command: "/project/fi-hook-to-otlp/emit.py" }],
      },
    }),
  );
  await assert.rejects(
    cursorChange("enable", "ide", c, o),
    /CURSOR_LEGACY_EMITTER_CONFLICT/,
  );
  fs.writeFileSync(o.hooksFile, '{"version":1,"hooks":{}}');
  await (await cursorChange("enable", "ide", c, o)).commit();
  const doc = parse(fs.readFileSync(o.hooksFile, "utf8"));
  doc.hooks.stop[0].command += " --changed";
  fs.writeFileSync(o.hooksFile, JSON.stringify(doc));
  await assert.rejects(
    cursorChange("disable", "ide", c, o),
    /CURSOR_CONFIG_CONFLICT/,
  );
});

test("rollback changes only owned registrations, never restores a whole secret-bearing config", async (t) => {
  const root = fixture(t),
    o = installOptions(root),
    c = installContext(root);
  const change = await cursorChange("enable", "ide", c, o);
  await change.commit();
  const d = parse(fs.readFileSync(o.hooksFile, "utf8"));
  d.concurrent = "keep";
  fs.writeFileSync(o.hooksFile, JSON.stringify(d));
  await change.rollback();
  const after = parse(fs.readFileSync(o.hooksFile, "utf8"));
  assert.equal(after.concurrent, "keep");
  assert.deepEqual(after.hooks, {});
});

test("packaged hook is neutral on malformed input, unsupported versions/events, missing runtime and oversize input", async (t) => {
  const root = fixture(t);
  for (const input of [
    "{",
    hook("preToolUse"),
    { ...hook(), cursor_version: "unknown" },
    hook(),
    "x".repeat(1048577),
  ]) {
    const result = await hookProcess(root, input);
    assert.deepEqual(result, { code: 0, stdout: "", stderr: "" });
  }
});

test("hook sends only a minimized source envelope, without reading transcript or credentials", async (t) => {
  const root = fixture(t),
    socketPath = path.join(root, "obs.sock");
  let resolveFrame;
  const got = new Promise((r) => (resolveFrame = r));
  const server = net.createServer((socket) => {
    let b = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      b = Buffer.concat([b, chunk]);
      if (b.length >= 4 && b.length >= b.readUInt32BE(0) + 4) {
        resolveFrame(JSON.parse(b.subarray(4).toString()));
        socket.end();
      }
    });
  });
  await new Promise((r) => server.listen(socketPath, r));
  t.after(() => new Promise((r) => server.close(r)));
  const result = await hookProcess(root, hook(), socketPath);
  assert.deepEqual(result, { code: 0, stdout: "", stderr: "" });
  const envelope = await Promise.race([
    got,
    new Promise((_, reject) => {
      const timer = setTimeout(
        () => reject(new Error("observer did not connect")),
        2000,
      );
      timer.unref();
    }),
  ]);
  assert.equal(envelope.operation, "observe-source");
  assert.equal(envelope.source_schema, HOOK_SCHEMA);
  assert.deepEqual(envelope.event, minimizeCursorEvent(hook(), "ide"));
  assert.ok(!JSON.stringify(envelope).includes(canary));
});

test("actual CLI enable/doctor/hook/status/disable works with synthetic auth in a throwaway home", async (t) => {
  const root = fs.mkdtempSync(path.join(base, "c-"));
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
      "test",
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
  assert.equal(auth.status, 0, auth.stdout + auth.stderr);
  const enabled = cli(root, [
    "enable",
    "cursor",
    "--surface",
    "ide",
    "--profile",
    "test",
    "--preview",
  ]);
  assert.equal(enabled.status, 0, enabled.stdout + enabled.stderr);
  let doctor = cli(root, ["doctor", "cursor", "--surface", "ide"]);
  assert.equal(JSON.parse(doctor.stdout).activation.activated, false);
  const result = await hookProcess(root, hook("stop"));
  assert.deepEqual(result, { code: 0, stdout: "", stderr: "" });
  // The foreground budget never promises FULL fsync. Off-path replay proves durable
  // admission without relaxing the hook's deadline or assuming a particular disk speed.
  const replay = await runtimeRequest(path.join(root, "s/runtime.sock"), {
    operation: "observe-source",
    route_ref: "cursor.ide",
    source_schema: HOOK_SCHEMA,
    event: minimizeCursorEvent(hook("stop"), "ide"),
  });
  assert.equal(replay.durable, true);
  doctor = cli(root, ["doctor", "cursor", "--surface", "ide"]);
  assert.equal(doctor.status, 0, doctor.stdout);
  assert.equal(
    JSON.parse(doctor.stdout).activation.activated,
    true,
    doctor.stdout,
  );
  assert.equal(JSON.parse(doctor.stdout).persistence, "unknown");
  const disabled = cli(root, ["disable", "cursor", "--surface", "ide"]);
  assert.equal(disabled.status, 0, disabled.stdout);
  assert.deepEqual(
    parse(fs.readFileSync(path.join(root, ".cursor/hooks.json"), "utf8")).hooks,
    {},
  );
  assert.equal(
    JSON.parse(cli(root, ["doctor", "cursor", "--surface", "ide"]).stdout)
      .activation.activated,
    false,
  );
  const reenabled = cli(root, [
    "enable",
    "cursor",
    "--surface",
    "ide",
    "--preview",
  ]);
  assert.equal(reenabled.status, 0, reenabled.stdout);
  assert.equal(
    JSON.parse(cli(root, ["doctor", "cursor", "--surface", "ide"]).stdout)
      .activation.activated,
    false,
    "a previous route epoch is not a new activation receipt",
  );
  const importEnabled = cli(root, [
    "enable",
    "cursor",
    "--surface",
    "cli",
    "--profile",
    "test",
    "--preview",
    "--mode",
    "import-only",
    "--transcript",
    "structure",
  ]);
  assert.equal(importEnabled.status, 0, importEnabled.stdout);
  const source = path.join(root, "stream.jsonl");
  fs.writeFileSync(source, bytes(stream()), { mode: 0o600 });
  const args = [
    "import",
    "cursor",
    "--surface",
    "cli",
    "--path",
    source,
    "--schema",
    STREAM_SCHEMA,
  ];
  const planned = cli(root, args);
  assert.equal(planned.status, 0, planned.stdout);
  assert.equal(JSON.parse(planned.stdout).counts.selected, 5);
  const committed = cli(root, [
    ...args,
    "--commit",
    JSON.parse(planned.stdout).plan_id,
  ]);
  assert.equal(committed.status, 3, committed.stdout);
  assert.match(committed.stdout, /HELD_UNPROVEN_SOURCE_ALIAS/);
  await assert.rejects(
    runtimeRequest(path.join(root, "s/runtime.sock"), {
      operation: "observe",
      route_ref: "cursor.cli",
      fact: readCursorStream(bytes(stream()), {
        ...context,
        sourceSchema: STREAM_SCHEMA,
      })[0],
    }),
    /HELD_UNPROVEN_SOURCE_ALIAS/,
  );
});
