const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {
  normalizeHook,
  turnUsage,
  GrokObserver,
  TranscriptCursor,
  grokAdapter,
} = require("../dist");
const config = {
  hideInputs: false,
  hideOutputs: false,
  hideErrorDetails: false,
  credentials: { apiKey: "known-api-value", secretKey: "known-secret-value" },
};
function tracer() {
  const spans = [];
  return {
    spans,
    startTurn(v) {
      const s = { kind: "root", ...v };
      spans.push(s);
      return {
        end(e) {
          Object.assign(s, e);
        },
        child(c) {
          const k = { ...c };
          spans.push(k);
          return {
            end(e) {
              Object.assign(k, e);
            },
          };
        },
      };
    },
  };
}
function event(hook, p = {}) {
  return normalizeHook(
    {
      hookEventName: hook,
      sessionId: "session-1",
      promptId: "turn-1",
      timestamp: "2026-11-06T00:00:00Z",
      ...p,
    },
    config,
  );
}
test("camel/snake aliases, credential redaction, content and error switches", () => {
  assert.equal(
    event("user_prompt_submit", { prompt: "known-api-value" }).input,
    "[REDACTED]",
  );
  assert.deepEqual(event("Stop"), event("stop"));
  assert.equal(
    normalizeHook(
      {
        hook_event_name: "PostToolUse",
        session_id: "session-1",
        tool_input: { secret: "known-secret-value" },
      },
      config,
    ).input,
    '{"secret":"[REDACTED]"}',
  );
  assert.equal(
    normalizeHook(
      { hookEventName: "UserPromptSubmit", sessionId: "s", prompt: "private" },
      { ...config, hideInputs: true },
    ).input,
    undefined,
  );
  assert.equal(
    normalizeHook(
      { hookEventName: "StopFailure", sessionId: "s", errorDetails: "private" },
      { ...config, hideErrorDetails: true },
    ).output,
    undefined,
  );
  assert.equal(
    event("UserPromptSubmit", { prompt: "a".repeat(30000) }).input.length,
    20000,
  );
  assert.equal(normalizeHook({ hookEventName: "Stop" }, config), undefined);
});
test("shell failures, denials and cancellations retain correct outcomes", () => {
  for (const toolResult of [
    { exit_code: 3 },
    { timed_out: true },
    { signal: 9 },
  ])
    assert.equal(event("PostToolUse", { toolResult }).outcome, "error");
  assert.equal(event("PermissionDenied").outcome, "error");
  assert.equal(event("StopCancelled").outcome, "interrupted");
});
test("one root per prompt, same conversation, alias dedup and neutral shutdown Stop", () => {
  const t = tracer(),
    o = new GrokObserver(t, config);
  o.handle(event("UserPromptSubmit"));
  o.handle(event("user_prompt_submit"));
  const p = {
    toolUseId: "call-1",
    toolName: "read_file",
    toolResult: "content",
    durationMs: 5,
  };
  o.handle(event("PostToolUse", p));
  o.handle(event("post_tool_use", p));
  o.handle(event("Stop", { promptId: undefined }));
  o.finish("session-1");
  assert.equal(o.active.size, 1);
  o.handle(event("Stop"));
  o.finish("session-1");
  o.handle(event("UserPromptSubmit", { promptId: "turn-2" }));
  o.handle(event("StopCancelled", { promptId: "turn-2" }));
  o.finish("session-1");
  assert.equal(t.spans.filter((s) => s.kind === "root").length, 2);
  assert.equal(t.spans.filter((s) => s.kind === "tool").length, 1);
  assert.equal(t.spans[2].outcome, "interrupted");
  assert.equal(t.spans[0].sessionId, t.spans[2].sessionId);
});
test("source turn usage appears once, no per-message token or raw tick dollar guesses", async () => {
  const t = tracer(),
    o = new GrokObserver(t, config),
    sid = "01a1b034-a61b-7362-a89b-d7fd85fb5be0",
    pid = "0aaaa957-05e8-4f5a-8698-86349bdb484b";
  o.handle(event("UserPromptSubmit", { sessionId: sid, promptId: pid }));
  const rows = (
    await fs.readFile(
      path.join(__dirname, "fixtures/updates-1.0.77.jsonl"),
      "utf8",
    )
  )
    .trim()
    .split("\n")
    .map(JSON.parse);
  for (const row of [...rows, ...rows]) o.transcript(row);
  o.handle(event("Stop", { sessionId: sid, promptId: pid }));
  o.finish(sid);
  assert.equal(t.spans[0].usage.input, 80);
  assert.equal(t.spans[0].usage.output, 20);
  assert.equal(
    t.spans.filter((s) => s.fidelity === "reconstructed-message").length,
    2,
  );
  assert.equal(t.spans.filter((s) => s.usage).length, 1);
  assert.ok(
    t.spans
      .filter((s) => s.fidelity === "reconstructed-message")
      .every((s) => s.model === "test"),
  );
  assert.equal(
    turnUsage({ inputTokens: 10, outputTokens: 2, cost_usd_ticks: 200 })
      .costUsd,
    undefined,
  );
});
test("verified subagent id nests child tools; compaction never starts a new root", () => {
  const t = tracer(),
    o = new GrokObserver(t, config);
  o.handle(event("UserPromptSubmit"));
  o.handle(
    event("SubagentStart", { promptId: undefined, subagentId: "child-1" }),
  );
  o.handle(
    event("UserPromptSubmit", {
      sessionId: "child-1",
      promptId: "child-turn",
      subagentType: "explore",
    }),
  );
  o.handle(
    event("PostToolUse", {
      sessionId: "child-1",
      promptId: undefined,
      toolUseId: "child-tool",
      toolName: "read_file",
    }),
  );
  o.handle(event("PostCompact", { sessionId: "child-1", promptId: undefined }));
  o.handle(
    event("SubagentStop", {
      sessionId: "child-1",
      promptId: "child-turn",
      phase: "gate",
    }),
  );
  o.finish("child-1");
  o.handle(event("Stop"));
  o.finish("session-1");
  assert.equal(t.spans.filter((s) => s.kind === "root").length, 1);
  assert.equal(
    t.spans.find((s) => s.kind === "tool").parentId,
    "subagent:child-1",
  );
  assert.equal(
    t.spans.find((s) => s.kind === "compaction").parentId,
    "subagent:child-1",
  );
});
test("real recorded hooks map without inventing missing child roots", async () => {
  const t = tracer(),
    o = new GrokObserver(t, config);
  for (const line of (
    await fs.readFile(
      path.join(__dirname, "fixtures/grok-1.0.52.jsonl"),
      "utf8",
    )
  )
    .trim()
    .split("\n")) {
    const row = JSON.parse(line),
      e = normalizeHook(row.payload, config);
    if (!e) continue;
    o.handle(e);
    if (
      ["Stop", "SubagentStop", "StopFailure", "StopCancelled"].includes(e.event)
    )
      o.finish(e.sessionId);
  }
  assert.equal(t.spans.filter((s) => s.kind === "root").length, 1);
  assert.ok(t.spans.some((s) => s.kind === "tool" && s.parentId));
});
test("bounded incremental transcript handles partial lines and refuses traversal/symlinks", async () => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "grok-reader-"));
  const dir = path.join(home, ".grok/sessions/project/sid");
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const file = path.join(dir, "updates.jsonl");
  await fs.writeFile(file, '{"first":1}\n{"second":');
  const cursor = new TranscriptCursor("sid", home),
    rows = [];
  await cursor.read(file, (r) => rows.push(r));
  assert.deepEqual(rows, [{ first: 1 }]);
  await fs.appendFile(file, "2}\n");
  await cursor.read(file, (r) => rows.push(r));
  assert.deepEqual(rows, [{ first: 1 }, { second: 2 }]);
  await cursor.read("/tmp/not-grok/updates.jsonl", () => assert.fail());
  assert.equal(cursor.gap, true);
  await fs.unlink(file);
  await fs.symlink("/etc/passwd", file);
  await assert.rejects(
    new TranscriptCursor("sid", home).read(file, () => assert.fail()),
  );
  await fs.rm(home, { recursive: true });
});
test("native hooks own bounded exports without a managed worker", () => {
  assert.equal(grokAdapter.requiresWorker, false);
});
test("malformed, oversized and disabled hooks are silent and fail open", async () => {
  const { spawn } = require("node:child_process");
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "grok-neutral-"));
  for (const input of [
    "not-json",
    "x".repeat(300000),
    JSON.stringify({
      hookEventName: "UserPromptSubmit",
      sessionId: "s",
      promptId: "p",
    }),
  ]) {
    const result = await new Promise((resolve) => {
      const p = spawn(
        process.execPath,
        [path.join(__dirname, "../dist/hook.js")],
        {
          env: {
            PATH: process.env.PATH,
            HOME: home,
            FI_AGENT_ENABLED: "false",
          },
        },
      );
      let out = "",
        err = "";
      p.stdout.on("data", (x) => (out += x));
      p.stderr.on("data", (x) => (err += x));
      p.stdin.on("error", () => {});
      p.stdin.end(input);
      p.on("close", (code) => resolve({ code, out, err }));
    });
    assert.deepEqual(result, { code: 0, out: "", err: "" });
  }
  await fs.rm(home, { recursive: true });
});
