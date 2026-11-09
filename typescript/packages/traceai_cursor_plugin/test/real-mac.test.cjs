const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  fixture,
  fs,
  path,
  child,
  spans,
  attributes,
  core,
} = require("./helpers.cjs");
const {
  installRuntime,
  register,
  unregister,
  commands,
  inspect,
} = require("../dist/registration.js");
const { normalizeEvent } = require("../dist/events.js");
const { appendEvent } = require("../dist/state.js");
const { detectHost } = require("../dist/index.js");
const load = (name) => require(`./fixtures/real-3.26.13-${name}.json`);

async function replayFixture(
  t,
  transcriptName = "real-3.26.13-transcript-1.jsonl",
) {
  const f = await fixture(t);
  const project = path.join(f.home, "volume", "project");
  await fs.mkdir(path.join(project, ".cursor"), {
    recursive: true,
    mode: 0o700,
  });
  // Reproduce the Mac external-volume ancestor. Its config must not reroute or disable user tracing.
  await fs.chmod(path.dirname(project), 0o775);
  await fs.writeFile(
    path.join(project, ".cursor", "futureagi.json"),
    JSON.stringify({
      schemaVersion: 1,
      enabled: false,
      projectName: "untrusted-project",
    }),
  );
  const transcript = path.join(
    f.home,
    ".cursor",
    "projects",
    "fixture",
    "agent-transcripts",
    "session.jsonl",
  );
  await fs.mkdir(path.dirname(transcript), { recursive: true, mode: 0o700 });
  await fs.writeFile(
    transcript,
    await fs.readFile(path.join(__dirname, "fixtures", transcriptName)),
    { mode: 0o600 },
  );
  const runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const hooks = commands(runtime, f.config.paths.configDir);
  const env = { HOME: f.home, PATH: "/missing", NODE_OPTIONS: "" };
  // Remap local control paths only. All other recorded payload fields remain unchanged.
  const send = async (raw, override = {}) => {
    const body = {
      ...raw,
      workspace_roots: [project],
      transcript_path: raw.transcript_path ? transcript : null,
      ...override,
    };
    const result = await child(hooks[body.hook_event_name].command, body, env);
    assert.equal(result.code, 0);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
    return result;
  };
  return { ...f, project, transcript, runtime, hooks, send };
}
function checkTree(all, expectedTools, generations) {
  const roots = all.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1);
  const root = roots[0];
  assert.equal(all.length, 1 + expectedTools.length + generations);
  const tools = all.filter((s) => attributes(s)["fi.span.kind"] === "TOOL");
  assert.deepEqual(tools.map((s) => s.name).sort(), [...expectedTools].sort());
  assert.equal(tools.filter((s) => s.name === "Write").length, 1);
  assert.equal(new Set(all.map((s) => s.spanId)).size, all.length);
  for (const s of all.filter((s) => s !== root)) {
    assert.equal(s.parentSpanId, root.spanId);
    assert.equal(s.traceId, root.traceId);
  }
  for (const tool of tools)
    assert.equal(attributes(tool)["tool.name"], tool.name);
  for (const key of Object.keys(attributes(root))) {
    if (generations) assert.ok(!key.startsWith("gen_ai.usage."));
  }
  return root;
}
function checkUsage(all, terminal, count) {
  const generations = all.filter(
    (s) => attributes(s)["fi.span.kind"] === "LLM",
  );
  assert.equal(generations.length, count);
  for (const g of generations) {
    assert.equal(attributes(g)["fi.agent.fidelity"], "reconstructed-message");
    assert.equal(attributes(g)["fi.agent.timing"], "reconstructed");
    assert.equal(attributes(g)["gen_ai.request.model"], terminal.model_id);
  }
  const bearers = generations.filter(
    (g) => attributes(g)["gen_ai.usage.input_tokens"] !== undefined,
  );
  assert.equal(bearers.length, 1);
  assert.equal(bearers[0].spanId, generations.at(-1).spanId);
  const a = attributes(bearers[0]);
  assert.equal(a["fi.agent.usage.additive"], true);
  for (const [source, target] of Object.entries({
    input_tokens: "input_tokens",
    output_tokens: "output_tokens",
    cache_read_tokens: "cache_read_tokens",
    cache_write_tokens: "cache_write_tokens",
  }))
    assert.equal(Number(a[`gen_ai.usage.${target}`]), terminal[source]);
  assert.equal(
    Number(a["gen_ai.usage.total_tokens"]),
    terminal.input_tokens + terminal.output_tokens,
  );
}

test("golden real run 3: first installed hook, 775 project, five tools, four LLM messages, usage once", async (t) => {
  const f = await replayFixture(t);
  const rows = load("run3");
  const first = await f.send(rows[0]);
  assert.ok(first.ms < 8000);
  assert.equal(spans(f.requests).length, 0);
  for (const row of rows.slice(1)) await f.send(row);
  const all = spans(f.requests);
  checkTree(all, ["Read", "Read", "Write", "Shell", "Shell"], 4);
  checkUsage(all, rows.at(-1), 4);
  assert.equal(
    all.filter((s) => s.name === "Shell" && s.status.code === 2).length,
    1,
  );
  await f.send(rows.at(-1));
  assert.equal(
    spans(f.requests).length,
    all.length,
    "duplicate stop cannot double-count usage",
  );
  const diagnostics = JSON.parse(
    await fs.readFile(
      path.join(f.config.paths.stateDir, "cursor-diagnostics.json"),
    ),
  );
  assert.ok(diagnostics.gaps.includes("PROJECT_CONFIG_IGNORED"));
  const status = await core.inspectStatus({
    ...f.options,
    projectDir: f.project,
  });
  assert.ok(status.issues.includes("PROJECT_CONFIG_IGNORED"));
  assert.equal(status.projectName, "cursor-test");
  const files = await fs.readdir(
    path.join(f.config.paths.stateDir, "cursor-turns"),
  );
  for (const file of files)
    assert.equal(
      (await fs.stat(path.join(f.config.paths.stateDir, "cursor-turns", file)))
        .mode & 0o777,
      0o600,
    );
});

test("golden real run 2 uses the transcript supplied by terminal hooks", async (t) => {
  const f = await replayFixture(t);
  for (const row of load("run2")) await f.send(row);
  checkTree(spans(f.requests), ["Grep", "Read", "Read", "Write", "Shell"], 5);
  checkUsage(spans(f.requests), load("run2").at(-1), 5);
  const diagnostics = JSON.parse(
    await fs.readFile(
      path.join(f.config.paths.stateDir, "cursor-diagnostics.json"),
    ),
  );
  assert.deepEqual(diagnostics.gaps, ["PROJECT_CONFIG_IGNORED"]);
});

test("run 2 without a host-supplied transcript never invents model generations", async (t) => {
  const f = await replayFixture(t);
  const rows = load("run2");
  for (const row of rows) await f.send(row, { transcript_path: null });
  checkTree(spans(f.requests), ["Grep", "Read", "Read", "Write", "Shell"], 0);
});

test("each real usage-limit turn exports one ERROR root; sessionEnd model_params is compatible", async (t) => {
  const f = await replayFixture(t, "real-3.26.13-transcript-0.jsonl");
  const rows = load("error");
  await f.send(rows[0]);
  // An aborted session with no prompt, tool work or usage is a diagnostic, not a trace.
  assert.equal(spans(f.requests).length, 0);
  await f.send(rows[0]);
  assert.equal(spans(f.requests).length, 0);
  for (const row of rows.slice(1, 3)) await f.send(row);
  // An error stop can precede transcript writes too. Preserve the ERROR fallback,
  // but wait for the one-shot deadline instead of expecting an incomplete export in the hook.
  const waitForRoots = async (count) => {
    const deadline = Date.now() + 31000;
    while (
      spans(f.requests).filter((s) => !s.parentSpanId).length < count &&
      Date.now() < deadline
    )
      await new Promise((r) => setTimeout(r, 50));
  };
  await waitForRoots(1);
  const all = spans(f.requests);
  assert.equal(all.length, 1);
  assert.equal(all[0].status.code, 2);
  for (const row of rows.slice(3)) await f.send(row);
  await waitForRoots(2);
  const both = spans(f.requests);
  assert.equal(both.length, 2);
  assert.equal(both[1].status.code, 2);
  assert.equal(
    attributes(both[0])["session.id"],
    attributes(both[1])["session.id"],
  );
  assert.notEqual(both[0].traceId, both[1].traceId);
});

test("known 3.x schema survives unknown event shape and diagnoses unsupported major", async (t) => {
  const f = await replayFixture(t);
  const rows = load("run3");
  await f.send(rows[0]);
  await f.send(rows[1], { tool_use_id: { changed: true } });
  await f.send(rows.at(-1));
  assert.equal(spans(f.requests).filter((s) => !s.parentSpanId).length, 1);
  let diagnostics = JSON.parse(
    await fs.readFile(
      path.join(f.config.paths.stateDir, "cursor-diagnostics.json"),
    ),
  );
  assert.ok(diagnostics.gaps.includes("UNKNOWN_CURSOR_SHAPE"));
  await f.send(rows[0], { cursor_version: "4.0.0" });
  diagnostics = JSON.parse(
    await fs.readFile(
      path.join(f.config.paths.stateDir, "cursor-diagnostics.json"),
    ),
  );
  assert.ok(diagnostics.gaps.includes("UNKNOWN_CURSOR_VERSION"));
});

test("concurrent real hooks retain every event; a killed lock owner is recovered", async (t) => {
  const f = await fixture(t);
  const rows = load("run3");
  const event = (row) => normalizeEvent(row, row.hook_event_name, f.config);
  await Promise.all(
    rows.slice(0, -1).map((row) => appendEvent(f.config, event(row))),
  );
  const dir = path.join(f.config.paths.stateDir, "cursor-turns");
  const file = (await fs.readdir(dir)).find((n) => n.endsWith(".json"));
  const lock = path.join(dir, file + ".mutex");
  // Legacy inner write lock left by a killed writer must not defeat recovery.
  await fs.writeFile(path.join(dir, file + ".lock"), "", { mode: 0o600 });
  await fs.mkdir(lock, { mode: 0o700 });
  const old = new Date(Date.now() - 5000);
  await fs.utimes(lock, old, old);
  const snapshot = await appendEvent(f.config, event(rows.at(-1)));
  assert.equal(snapshot.events.length, rows.length);
  assert.equal(snapshot.events.filter((e) => e.tool === "Write").length, 2);
  assert.equal(await appendEvent(f.config, event(rows.at(-1))), undefined);
});

test("concurrent subprocess hooks keep all real tool pairs", async (t) => {
  const f = await replayFixture(t);
  const rows = load("run3");
  await f.send(rows[0]);
  await Promise.all(rows.slice(1, -1).map((row) => f.send(row)));
  await f.send(rows.at(-1));
  checkTree(spans(f.requests), ["Read", "Read", "Write", "Shell", "Shell"], 4);
});

test("host version is data under a 775 Applications ancestor, never code", async (t) => {
  const f = await fixture(t);
  const applications = path.join(f.home, "Applications");
  const file = path.join(
    applications,
    "Cursor.app",
    "Contents",
    "Resources",
    "app",
    "package.json",
  );
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.chmod(applications, 0o775);
  const version = load("run3")[0].cursor_version;
  await fs.writeFile(file, JSON.stringify({ version }));
  assert.deepEqual(await detectHost([file]), {
    supported: true,
    version,
    reason: undefined,
  });
  await fs.writeFile(
    file,
    JSON.stringify({ version: "$(touch /tmp/never-execute)" }),
  );
  assert.equal((await detectHost([file])).supported, false);
});

test("nonterminal bundle has no exporter or OTel dependencies", async () => {
  const meta = JSON.parse(
    await fs.readFile(path.join(__dirname, "../runtime/hook.meta.json")),
  );
  assert.ok(
    !Object.keys(meta.inputs).some((p) =>
      /opentelemetry|fi-core|observer|tracer\.[jt]s/.test(p),
    ),
  );
});

test("runtime updates prune old Node copies and doctor detects a missing interpreter", async (t) => {
  const f = await fixture(t);
  const runtime = await installRuntime(f.context);
  const legacy = path.join(
    path.dirname(runtime),
    "cursor-0.1.0-01234567890123456789",
  );
  await fs.mkdir(legacy, { mode: 0o700 });
  await fs.writeFile(path.join(legacy, "node"), "old copied executable");
  await register(f.context, runtime);
  await assert.rejects(fs.stat(legacy), { code: "ENOENT" });
  await assert.rejects(fs.stat(path.join(runtime, "node")), { code: "ENOENT" });
  const receiptPath = path.join(
    f.config.paths.configDir,
    "cursor-registration.json",
  );
  const receipt = JSON.parse(await fs.readFile(receiptPath));
  receipt.interpreter = path.join(f.home, "removed-node");
  await core.writeProtected(receiptPath, JSON.stringify(receipt));
  assert.equal((await inspect(f.context)).detail, "CURSOR_INTERPRETER_MISSING");
  const { cursorAdapter } = require("../dist/index.js");
  const status = await core.inspectStatus({
    ...f.options,
    adapter: cursorAdapter,
  });
  assert.ok(status.issues.includes("CURSOR_INTERPRETER_MISSING"));
  await unregister(f.context);
  await assert.rejects(fs.stat(runtime), { code: "ENOENT" });
});

test("direct real run 3 observer preserves source messages", async (t) => {
  const f = await replayFixture(t);
  const { CursorObserver } = require("../dist/observer.js");
  const observer = new CursorObserver(f.config);
  for (const row of load("run3")) {
    const event = normalizeEvent(
      { ...row, transcript_path: f.transcript },
      row.hook_event_name,
      f.config,
    );
    await observer.accept(event);
  }
  await observer.close();
  assert.equal(
    spans(f.requests).length,
    10,
    JSON.stringify([...observer.gaps]),
  );
});
