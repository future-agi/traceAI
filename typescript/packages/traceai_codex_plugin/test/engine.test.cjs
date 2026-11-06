const { test } = require("node:test"),
  assert = require("node:assert/strict");
const { sanitizeEvent } = require("../dist/events.js"),
  { CodexEngine } = require("../dist/engine.js");
const config = {
  captureContent: true,
  hideInputs: false,
  hideOutputs: false,
  hideErrorDetails: false,
  credentials: { apiKey: "secret-api-key", secretKey: "secret-secret-key" },
};
const event = (name, rest = {}) =>
  sanitizeEvent(
    {
      hook_event_name: name,
      session_id: "s1",
      turn_id: "t1",
      cwd: "/work",
      ...rest,
    },
    config,
  );
function fake() {
  const spans = [];
  const tracer = {
    startTurn(v) {
      const root = { ...v, kind: "root" };
      spans.push(root);
      return {
        event(v) {
          (root.events ??= []).push(v);
        },
        child(v) {
          const s = { ...v };
          spans.push(s);
          return {
            end(e = {}) {
              s.end = e;
            },
          };
        },
        end(e) {
          root.end = e;
        },
      };
    },
    async flush() {},
    async shutdown() {},
  };
  return { spans, tracer };
}
test("capture default, bounded known-key redaction and structural tool failure", () => {
  const e = event("PostToolUse", {
    tool_response: "Process exited with code 7\nsecret-api-key",
    tool_input: "x".repeat(25000),
  });
  assert(e.failed);
  assert(!e.tool_response.includes("secret-api-key"));
  assert.equal(e.tool_input.length, 20000);
  const off = sanitizeEvent(
    {
      hook_event_name: "PostToolUse",
      session_id: "s",
      cwd: "/work",
      tool_response: "Exit code: 7",
      prompt: "secret",
    },
    { ...config, captureContent: false },
  );
  assert(off.failed);
  assert.equal(off.prompt, undefined);
  assert.equal(off.tool_response, undefined);
  assert.equal(event("PermissionRequest"), undefined);
});
test("one turn, attempt-owned usage, model retry, nonzero shell error and final Stop", async () => {
  const { spans, tracer } = fake(),
    engine = new CodexEngine(config, tracer);
  for (const e of [
    event("UserPromptSubmit", { prompt: "test" }),
    event("PreModelRequest"),
    event("PostModelRequest", { status: "failed", error: "retry" }),
    event("PreModelRequest"),
    event("PostModelRequest", {
      status: "completed",
      token_usage: {
        input_tokens: 20,
        output_tokens: 8,
        cached_input_tokens: 4,
      },
    }),
    event("PreToolUse", {
      tool_use_id: "call1",
      tool_name: "shell",
      tool_input: { command: "exit 7" },
    }),
    event("PostToolUse", {
      tool_use_id: "call1",
      tool_response: "Process exited with code 7",
    }),
    event("Stop", { last_assistant_message: "done" }),
  ])
    await engine.accept(e);
  assert.equal(spans.filter((s) => s.kind === "root").length, 1);
  assert.equal(spans.filter((s) => s.kind === "llm").length, 2);
  assert.equal(spans[0].usagePlan, "attempt");
  assert.equal(spans[0].end.usage, undefined);
  assert.equal(spans[1].end.outcome, "error");
  assert.equal(spans[2].end.usage.input, 20);
  assert.equal(spans[3].end.outcome, "error");
  await engine.accept(event("Stop"));
  assert.equal(spans.length, 4);
});
test("compaction does not start a prompt; interruption not success; missing stops incomplete", async () => {
  const { spans, tracer } = fake(),
    engine = new CodexEngine(config, tracer);
  for (const n of [
    "UserPromptSubmit",
    "PreCompact",
    "PostCompact",
    "PreModelRequest",
    "Interrupt",
  ])
    await engine.accept(event(n));
  assert.equal(spans.filter((s) => s.kind === "root").length, 1);
  assert.equal(spans[1].kind, "compaction");
  assert.equal(spans[0].end.outcome, "interrupted");
  await engine.accept(event("UserPromptSubmit", { turn_id: "t2" }));
  await engine.close();
  assert.equal(spans.at(-1).end.outcome, "incomplete");
});
test("explicit subagent lifecycle nests known child model hooks", async () => {
  const { spans, tracer } = fake(),
    engine = new CodexEngine(config, tracer);
  for (const e of [
    event("UserPromptSubmit"),
    event("SubagentStart", { agent_id: "child" }),
    event("PreModelRequest", { agent_id: "child" }),
    event("PostModelRequest", { agent_id: "child", status: "completed" }),
    event("SubagentStop", { agent_id: "child" }),
    event("Stop"),
  ])
    await engine.accept(e);
  assert.equal(spans[2].parentId, "agent:child");
  assert.equal(spans.filter((s) => s.kind === "root").length, 1);
});
test("reused child has distinct execution parents and source-turn prompt attribution", async () => {
  const { spans, tracer } = fake(),
    engine = new CodexEngine(config, tracer);
  await engine.accept(event("UserPromptSubmit", { prompt: "parent prompt" }));
  for (const [source, prompt] of [
    ["c1", "first child prompt"],
    ["c2", "second child prompt"],
  ]) {
    const child = {
      agent_id: "child",
      source_session_id: "child",
      source_turn_id: source,
    };
    if (source === "c1")
      await engine.accept({ ...event("SubagentStart"), ...child });
    for (const name of [
      "UserPromptSubmit",
      "PreModelRequest",
      "PostModelRequest",
      "SubagentStop",
    ])
      await engine.accept({
        ...event(name, { prompt, status: "completed" }),
        ...child,
      });
  }
  await engine.accept(event("Stop"));
  const generations = spans.filter((s) => s.kind === "llm");
  assert.deepEqual(
    generations.map((s) => s.input),
    ["first child prompt", "second child prompt"],
  );
  assert.deepEqual(
    generations.map((s) => s.parentId),
    ["agent:child:c1", "agent:child:c2"],
  );
  assert.equal(spans.filter((s) => s.kind === "agent").length, 2);
});
