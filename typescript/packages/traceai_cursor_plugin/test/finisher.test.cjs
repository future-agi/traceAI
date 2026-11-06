const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const {
  fixture,
  fs,
  path,
  child,
  payload,
  core,
  spans,
  attributes,
} = require("./helpers.cjs");
const {
  installRuntime,
  register,
  commands,
} = require("../dist/registration.js");
const { cursorAdapter } = require("../dist/index.js");
const {
  finisherStatus,
  appendEvent,
  pendingTurnIds,
  claimPending,
  finishPending,
} = require("../dist/state.js");
const { normalizeEvent } = require("../dist/events.js");
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const exited = (pid) => {
  try {
    process.kill(pid, 0);
    return false;
  } catch (error) {
    return error.code === "ESRCH";
  }
};
async function until(fn, budget = 5000) {
  const end = Date.now() + budget;
  do {
    if (await fn()) return;
    await pause(50);
  } while (Date.now() < end);
  assert.fail("condition did not settle within deadline");
}
async function pendingFixture(t) {
  const f = await fixture(t),
    runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const hooks = commands(runtime, f.config.paths.configDir);
  const transcript = path.join(
    f.home,
    ".cursor/projects/late/agent-transcripts/turn.jsonl",
  );
  await fs.mkdir(path.dirname(transcript), { recursive: true, mode: 0o700 });
  const text =
    [
      { role: "user", message: { content: "LATE_PRIVATE_PROMPT" } },
      {
        role: "assistant",
        message: {
          content: [
            { type: "tool_use", name: "Read", input: { path: "safe" } },
          ],
        },
      },
      { role: "assistant", message: { content: "LATE_PRIVATE_OUTPUT" } },
      { type: "turn_ended" },
    ]
      .map(JSON.stringify)
      .join("\n") + "\n";
  const env = { HOME: f.home, PATH: "/missing", NODE_OPTIONS: "" };
  const send = async (name, extra = {}, extraEnv = {}) => {
    const result = await child(
      hooks[name].command,
      payload(name, { transcript_path: transcript, ...extra }),
      { ...env, ...extraEnv },
    );
    assert.equal(result.code, 0);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
    return result;
  };
  for (const [name, data] of [
    ["beforeSubmitPrompt", { prompt: "LATE_PRIVATE_PROMPT" }],
    [
      "postToolUse",
      { tool_name: "Read", tool_use_id: "tool", tool_output: "safe" },
    ],
    ["afterAgentResponse", { text: "LATE_PRIVATE_OUTPUT" }],
  ])
    await send(name, data);
  const stop = () =>
    send("stop", { status: "completed", input_tokens: 100, output_tokens: 10 });
  const end = () =>
    send("sessionEnd", { generation_id: "session-1", reason: "completed" });
  const state = async () => {
    const dir = path.join(f.config.paths.stateDir, "cursor-turns");
    const name = (await fs.readdir(dir)).find((n) => n.endsWith(".json"));
    return {
      file: path.join(dir, name),
      id: name.slice(0, -5),
      value: JSON.parse(await fs.readFile(path.join(dir, name))),
    };
  };
  return { ...f, runtime, transcript, text, send, stop, end, state, env };
}
function checkFull(f) {
  const all = spans(f.requests),
    roots = all.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1);
  assert.equal(all.length, 4);
  const generations = all.filter(
    (s) => attributes(s)["fi.span.kind"] === "LLM",
  );
  assert.equal(generations.length, 2);
  const bearers = all.filter(
    (s) => attributes(s)["gen_ai.usage.total_tokens"] !== undefined,
  );
  assert.equal(bearers.length, 1);
  assert.equal(bearers[0].spanId, generations.at(-1).spanId);
  assert.equal(
    Number(attributes(bearers[0])["gen_ai.usage.total_tokens"]),
    110,
  );
  assert.equal(attributes(roots[0])["gen_ai.usage.total_tokens"], undefined);
}
for (const delay of [6000, 12000])
  test(`detached finisher completes ${delay}ms late transcript exactly once`, async (t) => {
    const f = await pendingFixture(t);
    const writer = pause(delay).then(() =>
      fs.writeFile(f.transcript, f.text, { mode: 0o600 }),
    );
    t.after(() => writer);
    const started = Date.now(),
      result = await f.stop();
    assert.ok(result.ms < 3000, `hook took ${result.ms}ms`);
    assert.equal(spans(f.requests).length, 0);
    const state = await f.state();
    assert.equal(state.value.phase, "pending");
    assert.equal((await fs.stat(state.file)).mode & 0o777, 0o600);
    assert.ok(!JSON.stringify(state.value).includes("LATE_PRIVATE"));
    const status = await core.inspectStatus({
      ...f.options,
      adapter: cursorAdapter,
    });
    assert.equal(status.finisher.pending.length, 1);
    assert.ok(status.finisher.pending[0].startedAt);
    const firstPid = state.value.finisher.pid;
    await Promise.all([f.stop(), f.stop(), f.end()]);
    assert.equal((await f.state()).value.finisher.pid, firstPid);
    assert.equal(spans(f.requests).length, 0);
    await writer;
    await until(() => spans(f.requests).length === 4);
    await until(
      async () =>
        (await finisherStatus(f.config)).last?.result === "EXPORTED_FULL",
    );
    checkFull(f);
    await until(() => exited(firstPid));
    assert.equal((await f.state()).value.encrypted, undefined);
    assert.equal(
      (await core.inspectStatus({ ...f.options, adapter: cursorAdapter }))
        .finisher.pending.length,
      0,
    );
    await f.end();
    assert.equal(spans(f.requests).length, 4);
    console.log(
      JSON.stringify({
        evidence: "late-finisher",
        delay,
        hookMs: result.ms,
        completedMs: Date.now() - started,
        spans: 4,
      }),
    );
  });
test("never-written transcript falls back at the hard cap with additive root usage and destroys content", async (t) => {
  const f = await pendingFixture(t),
    started = Date.now();
  await f.stop();
  const pid = (await f.state()).value.finisher.pid;
  await until(
    async () =>
      (await finisherStatus(f.config)).last?.result === "EXPORTED_FALLBACK",
    31000,
  );
  await until(() => exited(pid));
  const elapsed = Date.now() - started;
  assert.ok(elapsed >= 28000 && elapsed < 32500, `fallback took ${elapsed}ms`);
  const all = spans(f.requests),
    root = all.find((s) => !s.parentSpanId);
  assert.equal(all.length, 2);
  assert.equal(attributes(root)["fi.agent.gap"], "TRANSCRIPT_UNAVAILABLE");
  assert.equal(attributes(root)["fi.usage.contribution"], "additive");
  assert.equal(Number(attributes(root)["gen_ai.usage.total_tokens"]), 110);
  assert.equal((await f.state()).value.encrypted, undefined);
  await f.end();
  assert.equal(spans(f.requests).length, 2);
  console.log(
    JSON.stringify({ evidence: "finisher-hard-cap", elapsed, spans: 2 }),
  );
});
test("disable during the detached wait fences export and erases content", async (t) => {
  const f = await pendingFixture(t);
  await f.stop();
  await core.disable(f.options);
  await fs.writeFile(f.transcript, f.text, { mode: 0o600 });
  await until(
    async () =>
      (await finisherStatus(f.config)).last?.result ===
      "CANCELLED_CONFIG_OR_DISABLE",
  );
  assert.equal(spans(f.requests).length, 0);
  assert.equal((await f.state()).value.encrypted, undefined);
});
test("privacy is rechecked at export after the finisher starts", async (t) => {
  const f = await pendingFixture(t);
  await f.stop();
  await core.writeProtected(
    f.config.paths.user,
    JSON.stringify({ schemaVersion: 1, captureContent: false }),
  );
  await fs.writeFile(f.transcript, f.text, { mode: 0o600 });
  await until(
    async () =>
      (await finisherStatus(f.config)).last?.result ===
      "CANCELLED_CONFIG_OR_DISABLE",
  );
  assert.equal(spans(f.requests).length, 0);
  assert.equal((await f.state()).value.encrypted, undefined);
});
for (const policy of ["captureContent", "enabled", "routing", "invalid"])
  test(`${policy} change during export-claim contention cancels and erases the detached snapshot`, async (t) => {
    const f = await pendingFixture(t);
    await f.stop();
    // Let the spawned process acquire its initial claim before holding the export mutex.
    await pause(600);
    const state = await f.state();
    const { release } = await require("@traceai/agent-core/file-lock").acquireWriteLock(state.file);
    try {
      await fs.writeFile(f.transcript, f.text, { mode: 0o600 });
      await pause(700);
      assert.equal((await f.state()).value.phase, "pending");
      assert.equal(spans(f.requests).length, 0);
      const change = {
        captureContent: { schemaVersion: 1, captureContent: false },
        enabled: { schemaVersion: 1, enabled: false },
        routing: { schemaVersion: 1, projectName: "revoked-route" },
        invalid: { schemaVersion: 1, captureContent: "invalid" },
      }[policy];
      await core.writeProtected(f.config.paths.user, JSON.stringify(change));
    } finally {
      await release();
    }
    await until(
      async () =>
        (await finisherStatus(f.config)).last?.result ===
        "CANCELLED_CONFIG_OR_DISABLE",
    );
    await until(() => exited(state.value.finisher.pid));
    assert.equal(spans(f.requests).length, 0);
    assert.equal((await f.state()).value.encrypted, undefined);
  });
test("privacy revoked after the export claim is rechecked at the actual delivery boundary", async (t) => {
  const f = await pendingFixture(t);
  const { processPending } = require("../dist/runtime.js");
  const { TranscriptReader } = require("../dist/transcript.js");
  await appendEvent(
    f.config,
    normalizeEvent(
      payload("stop", { status: "completed", transcript_path: f.transcript }),
      "stop",
      f.config,
    ),
    { defer: true },
  );
  await fs.writeFile(f.transcript, f.text, { mode: 0o600 });
  const original = TranscriptReader.prototype.read;
  const home = process.env.HOME;
  process.env.HOME = f.home;
  let reads = 0;
  TranscriptReader.prototype.read = async function (...args) {
    const result = await original.apply(this, args);
    // First read settles the transcript; second is the observer replaying the prompt,
    // after the claim and its privacy check but before the terminal flush.
    if (++reads === 2) {
      assert.equal((await f.state()).value.finisher.result, "EXPORT_CLAIMED");
      await core.writeProtected(
        f.config.paths.user,
        JSON.stringify({ schemaVersion: 1, captureContent: false }),
      );
    }
    return result;
  };
  try {
    await processPending(f.config);
  } finally {
    TranscriptReader.prototype.read = original;
    if (home === undefined) delete process.env.HOME;
    else process.env.HOME = home;
  }
  assert.equal(reads, 2);
  assert.equal(spans(f.requests).length, 0);
  assert.equal((await f.state()).value.encrypted, undefined);
  assert.equal(
    (await finisherStatus(f.config)).last.result,
    "CANCELLED_CONFIG_OR_DISABLE",
  );
});
test("finisher crash before export is recovered by the next hook without duplicate delivery", async (t) => {
  const f = await pendingFixture(t);
  await f.stop();
  const state = await f.state();
  process.kill(state.value.finisher.pid, "SIGKILL");
  await pause(150);
  assert.ok((await f.state()).value.encrypted);
  assert.equal(spans(f.requests).length, 0);
  await fs.writeFile(f.transcript, f.text, { mode: 0o600 });
  await Promise.all([f.stop(), f.end()]);
  await until(() => spans(f.requests).length === 4);
  checkFull(f);
  assert.equal((await f.state()).value.encrypted, undefined);
  await f.stop();
  assert.equal(spans(f.requests).length, 4);
});
test("expired claim fences a stale owner and export claims cannot be replayed", async (t) => {
  const f = await pendingFixture(t);
  const terminal = normalizeEvent(
    payload("stop", { status: "completed" }),
    "stop",
    f.config,
  );
  await appendEvent(f.config, terminal, { defer: true });
  const [id] = await pendingTurnIds(f.config);
  const original = await claimPending(f.config, id);
  assert.ok(original);
  assert.equal(await claimPending(f.config, id), undefined);
  const state = await f.state();
  state.value.finisher.deadline = Date.now() - 1;
  await core.writeProtected(state.file, JSON.stringify(state.value));
  const recovered = await claimPending(f.config, id);
  assert.notEqual(recovered.token, original.token);
  assert.equal(
    recovered.deadline,
    state.value.finisher.deadline,
    "recovery never resets the cap",
  );
  assert.equal(
    await finishPending(f.config, original, "EXPORT_CLAIMED"),
    false,
  );
  assert.equal(await claimPending(f.config, id, original.token), undefined);
  assert.equal(
    await finishPending(f.config, recovered, "EXPORT_CLAIMED"),
    true,
  );
  assert.equal((await f.state()).value.encrypted, undefined);
  assert.equal(await claimPending(f.config, id), undefined);
  await Promise.all([f.stop(), f.end()]);
  assert.equal(
    spans(f.requests).length,
    0,
    "never retry an ambiguous claimed export",
  );
});
test("disable before child startup cancels the old generation and removes content", async (t) => {
  const f = await pendingFixture(t);
  const terminal = normalizeEvent(
    payload("stop", { status: "completed" }),
    "stop",
    f.config,
  );
  await appendEvent(f.config, terminal, { defer: true });
  const [id] = await pendingTurnIds(f.config);
  const claim = await claimPending(f.config, id);
  await core.disable(f.options);
  await new Promise((resolve, reject) => {
    const p = spawn(
      process.execPath,
      [
        path.join(f.runtime, "observer.cjs"),
        "--finish",
        f.config.paths.configDir,
        id,
        claim.token,
      ],
      { env: f.env, stdio: "ignore" },
    );
    p.once("error", reject);
    p.once("exit", (code) => {
      assert.equal(code, 0);
      resolve();
    });
  });
  assert.equal(
    (await finisherStatus(f.config)).last.result,
    "CANCELLED_CONFIG_OR_DISABLE",
  );
  assert.equal((await f.state()).value.encrypted, undefined);
  assert.equal(spans(f.requests).length, 0);
});
test("kill switch is respected by the standalone finisher as well as the hook", async (t) => {
  const f = await pendingFixture(t);
  await f.send("stop", { status: "completed" }, { FI_AGENT_ENABLED: "false" });
  assert.equal((await f.state()).value.phase, "collecting");
  await f.stop();
  const state = await f.state();
  process.kill(state.value.finisher.pid, "SIGKILL");
  await pause(100);
  await fs.writeFile(f.transcript, f.text, { mode: 0o600 });
  await new Promise((resolve, reject) => {
    const p = spawn(
      process.execPath,
      [
        path.join(f.runtime, "observer.cjs"),
        "--finish",
        f.config.paths.configDir,
        state.id,
        state.value.finisher.token,
      ],
      { env: { ...f.env, FI_AGENT_ENABLED: "false" }, stdio: "ignore" },
    );
    p.once("error", reject);
    p.once("exit", (code) => {
      assert.equal(code, 0);
      resolve();
    });
  });
  assert.equal(
    (await finisherStatus(f.config)).last.result,
    "CANCELLED_CONFIG_OR_DISABLE",
  );
  assert.equal(spans(f.requests).length, 0);
  assert.equal((await f.state()).value.encrypted, undefined);
});
