const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  fixture,
  payload,
  spans,
  attributes,
  core,
  fs,
  path,
} = require("./helpers.cjs");
const { CursorObserver, normalizeEvent } = require("../dist/index.js");

test("real sanitized 3.24.18 fixture replay emits one prompt root and two tools, never invented model requests", async (t) => {
  const f = await fixture(t),
    observer = new CursorObserver(f.config);
  const fixtures = (
    await fs.readFile(
      path.join(__dirname, "fixtures/real-3.24.18-turn.jsonl"),
      "utf8",
    )
  )
    .trim()
    .split("\n")
    .map(JSON.parse);
  for (const row of fixtures) {
    if (["afterAgentThought"].includes(row.event)) continue;
    const event = normalizeEvent(row.payload, row.event, f.config);
    await observer.accept(event);
    await observer.accept(event); // replay and compatibility duplication
  }
  await observer.close();
  const exported = spans(f.requests),
    roots = exported.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1);
  assert.equal(exported.length, 3);
  const root = roots[0],
    attr = attributes(root);
  assert.equal(attr["fi.span.kind"], "AGENT");
  assert.match(attr["input.value"], /PROMPT_MARKER_REAL/);
  assert.match(attr["output.value"], /RESPONSE_MARKER_REAL/);
  assert.equal(attr["fi.agent.summary.input"], undefined);
  assert.ok(!("llm.token_count.prompt" in attr));
  assert.equal(attr["fi.agent.usage.additive"], true);
  assert.equal(attr["fi.usage.contribution"], "additive");
  const reported = fixtures.find((r) => r.event === "stop").payload;
  assert.equal(
    Number(attr["gen_ai.usage.input_tokens"]),
    reported.input_tokens,
  );
  assert.equal(
    Number(attr["gen_ai.usage.output_tokens"]),
    reported.output_tokens,
  );
  assert.ok(exported.every((s) => attributes(s)["fi.span.kind"] !== "LLM"));
  assert.ok(
    exported
      .filter((s) => s.parentSpanId)
      .every(
        (s) => s.parentSpanId === root.spanId && s.traceId === root.traceId,
      ),
  );
  const body = JSON.stringify(f.requests.map((r) => r.body));
  assert.ok(!body.includes("fixture.user@example.com"));
  assert.ok(!body.includes("transcript-CANARY"));
  assert.ok(observer.gaps.has("TRANSCRIPT_PATH_REJECTED"));
});
test("two turns share a session; exact source ids remain distinct, including newline tool ids", async (t) => {
  const f = await fixture(t),
    observer = new CursorObserver(f.config);
  for (const id of ["one", "two"])
    for (const [event, extra] of [
      ["beforeSubmitPrompt", { prompt: id }],
      [
        "postToolUse",
        {
          tool_name: "Read",
          tool_use_id: "a\nb",
          tool_input: { file: "hello" },
          tool_output: "world",
        },
      ],
      [
        "postToolUse",
        { tool_name: "Read", tool_use_id: "a", tool_output: "another" },
      ],
      ["stop", { status: "completed" }],
    ])
      await observer.accept(
        normalizeEvent(
          payload(event, { generation_id: id, ...extra }),
          event,
          f.config,
        ),
      );
  await observer.close();
  const all = spans(f.requests),
    roots = all.filter((s) => !s.parentSpanId);
  assert.equal(all.length, 6);
  assert.equal(roots.length, 2);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.equal(
    attributes(roots[0])["session.id"],
    attributes(roots[1])["session.id"],
  );
});
test("explicit subagent ancestry, compaction and interrupted status are retained without guessing", async (t) => {
  const f = await fixture(t),
    o = new CursorObserver(f.config);
  for (const [event, extra] of [
    ["beforeSubmitPrompt", { prompt: "explore" }],
    ["preToolUse", { tool_use_id: "task-1", tool_name: "Task" }],
    [
      "subagentStart",
      { subagent_id: "child", parent_tool_use_id: "task-1", task: "look" },
    ],
    [
      "subagentStop",
      {
        subagent_id: "child",
        parent_tool_use_id: "task-1",
        status: "completed",
        summary: "done",
      },
    ],
    ["subagentStop", { status: "completed" }],
    ["preCompact", {}],
    ["postToolUse", { tool_use_id: "task-1", tool_name: "Task" }],
    ["stop", { status: "aborted" }],
  ])
    await o.accept(normalizeEvent(payload(event, extra), event, f.config));
  await o.close();
  const all = spans(f.requests),
    root = all.find((s) => !s.parentSpanId),
    tool = all.find((s) => s.name === "Task"),
    child = all.find((s) => s.name === "subagent");
  assert.equal(child.parentSpanId, tool.spanId);
  assert.equal(tool.parentSpanId, root.spanId);
  assert.equal(attributes(root)["fi.agent.outcome"], "interrupted");
  assert.equal(
    attributes(all.find((s) => s.name === "compaction"))["fi.agent.outcome"],
    "incomplete",
  );
  assert.ok(o.gaps.has("UNPROVEN_SUBAGENT_PARENT"));
});
test("content opt-out, credential redaction, error hiding and unsupported version handling", async (t) => {
  const f = await fixture(t, false);
  const e = normalizeEvent(
    payload("postToolUseFailure", {
      tool_use_id: "t",
      tool_input: "SECRET",
      error_message: "SECRET",
      transcript_path: "/arbitrary",
    }),
    "postToolUseFailure",
    f.config,
  );
  assert.equal(e.input, undefined);
  assert.equal(e.output, undefined);
  assert.equal(e.transcript, undefined);
  const config = {
    ...f.config,
    credentials: f.config.credentials,
    captureContent: true,
    hideInputs: false,
    hideOutputs: false,
    hideErrorDetails: true,
  };
  const hidden = normalizeEvent(
    payload("postToolUseFailure", {
      tool_use_id: "t",
      tool_input: "synthetic-cursor-secret",
      error_message: "ERROR",
    }),
    "postToolUseFailure",
    config,
  );
  assert.equal(hidden.input, "[REDACTED]");
  assert.equal(hidden.output, undefined);
  const shell = normalizeEvent(
    payload("postToolUse", {
      tool_use_id: "t",
      tool_output: '{"exitCode":1,"stderr":"ERROR"}',
    }),
    "postToolUse",
    config,
  );
  assert.equal(shell.outcome, "error");
  assert.equal(shell.output, undefined);
  assert.throws(
    () =>
      normalizeEvent(
        payload("stop", { cursor_version: "999.0.0" }),
        "stop",
        config,
      ),
    /UNKNOWN_CURSOR_VERSION/,
  );
});
test("persistent disable fences active turns; a source-identified terminal retains a missing-prompt root", async (t) => {
  const f = await fixture(t),
    o = new CursorObserver(f.config);
  await o.accept(
    normalizeEvent(payload("stop", { status: "completed" }), "stop", f.config),
  );
  assert.ok(o.gaps.has("MISSING_PROMPT"));
  assert.equal(spans(f.requests).length, 1);
  assert.equal(attributes(spans(f.requests)[0])["input.value"], undefined);
  await o.accept(
    normalizeEvent(
      payload("beforeSubmitPrompt", { generation_id: "next", prompt: "hello" }),
      "beforeSubmitPrompt",
      f.config,
    ),
  );
  await core.disable(f.options);
  await o.accept(
    normalizeEvent(
      payload("stop", { generation_id: "next", status: "completed" }),
      "stop",
      f.config,
    ),
  );
  await o.close();
  assert.equal(spans(f.requests).length, 1);
});
