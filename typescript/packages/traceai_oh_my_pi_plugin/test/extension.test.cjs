const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const plugin = require("../dist/index.js").default;
const { ompUsage, boundedJson } = require("../dist/content.js");
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
async function harness(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "omp-unit-"));
  const batches = [];
  const server = http.createServer(async (req, res) => {
    let text = "";
    for await (const b of req) text += b;
    batches.push(JSON.parse(text));
    res.setHeader("content-type", "application/json");
    res.end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const old = { ...process.env };
  Object.assign(process.env, {
    HOME: home,
    FI_AGENT_ENABLED: "true",
    PI_CODING_AGENT_DIR: path.join(home, "agent"),
    FI_AGENT_CONFIG_DIR: path.join(home, "fi"),
    XDG_STATE_HOME: path.join(home, "state"),
    FI_API_KEY: "synthetic-api",
    FI_SECRET_KEY: "synthetic-secret",
    FI_PROJECT_NAME: "unit",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  });
  const handlers = {},
    bus = {};
  plugin({
    on(n, f) {
      handlers[n] = f;
    },
    events: {
      on(n, f) {
        bus[n] = f;
        return () => delete bus[n];
      },
    },
  });
  let session = "conversation";
  const ctx = {
    cwd: home,
    agent: { kind: "main", id: "Main" },
    sessionManager: { getSessionId: () => session },
    model: { id: "test", provider: "mock" },
  };
  const emit = async (type, e = {}) => handlers[type]?.({ type, ...e }, ctx);
  const spans = () =>
    batches.flatMap((b) =>
      b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
  t.after(async () => {
    await emit("session_shutdown");
    await new Promise((r) => server.close(r));
    for (const k of Object.keys(process.env))
      if (!(k in old)) delete process.env[k];
    Object.assign(process.env, old);
    await fs.rm(home, { recursive: true, force: true });
  });
  await emit("session_start");
  return {
    emit,
    spans,
    ctx,
    bus,
    home,
    handlers,
    batches,
    switchSession(v) {
      session = v;
    },
  };
}
const assistant = (text = "answer", timestamp = Date.now()) => ({
  message: {
    role: "assistant",
    timestamp,
    completedAt: timestamp + 10,
    model: "test",
    provider: "mock",
    content: [{ type: "text", text }],
    stopReason: "stop",
    usage: {
      input: 10,
      output: 4,
      cacheRead: 2,
      cacheWrite: 1,
      cost: { total: 0.01 },
    },
  },
});
test("one dequeued prompt per trace without input, session grouping, no provisional reexports", async (t) => {
  const h = await harness(t);
  for (const prompt of ["PRINT", "QUEUED"]) {
    await h.emit("before_agent_start", { reason: "prompt", prompt });
    await h.emit("before_provider_request", {
      payload: {
        messages: [{ role: "user", content: prompt }],
        headers: { authorization: "do-not-export" },
      },
    });
    await h.emit("message_end", assistant(prompt));
    await h.emit("agent_end");
  }
  await h.emit("session_shutdown");
  const spans = h.spans();
  assert.equal(spans.length, 4);
  const roots = spans.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 2);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.equal(attrs(roots[0])["session.id"], attrs(roots[1])["session.id"]);
  assert.equal(new Set(spans.map((s) => s.spanId)).size, 4);
  assert(!JSON.stringify(spans).includes("do-not-export"));
  assert.equal(
    spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    2,
  );
});
test("preparation retries coalesce, abandoned preparation exports nothing, identical queued prompts stay distinct", async (t) => {
  const h = await harness(t);
  for (let i = 0; i < 2; i++) {
    await h.emit("before_agent_start", {
      reason: "prompt",
      prompt: "DISCARDED_PREPARATION",
    });
    await h.emit("before_agent_start", {
      reason: "prompt",
      prompt: "SAME_PROMPT",
    });
    await h.emit("agent_start");
    await h.emit("message_end", assistant("answer", 1000 + i));
    await h.emit("agent_end");
  }
  await h.emit("before_agent_start", {
    reason: "prompt",
    prompt: "ABANDONED_PREPARATION",
  });
  await h.emit("session_shutdown");
  const spans = h.spans(),
    roots = spans.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 2);
  assert.equal(new Set(roots.map((s) => s.traceId)).size, 2);
  assert.equal(
    spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    2,
  );
  assert(roots.every((s) => attrs(s)["input.value"] === "SAME_PROMPT"));
  assert(!JSON.stringify(spans).includes("PREPARATION"));
});
test("ambiguous provider callbacks cannot fabricate retries or unfinished advisor generations", async (t) => {
  const h = await harness(t);
  await h.emit("before_agent_start", { reason: "prompt", prompt: "ACP" });
  await h.emit("message_end", assistant("no callback"));
  await h.emit("before_provider_request", { payload: { messages: [] } });
  await h.emit("before_provider_request", { payload: { messages: [] } });
  await h.emit("message_end", assistant("retry", Date.now() + 100));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const spans = h.spans();
  assert.equal(spans.length, 3);
  assert.equal(
    spans.filter(
      (s) => attrs(s)["fi.agent.fidelity"] === "reconstructed-message",
    ).length,
    2,
  );
  assert.equal(
    spans.filter((s) => attrs(s)["fi.agent.outcome"] === "incomplete").length,
    0,
  );
  const root = spans.find((s) => !s.parentSpanId);
  assert.equal(
    attrs(root)["fi.agent.gap"],
    "AUXILIARY_GENERATIONS_UNAVAILABLE",
  );
});
test("native child ancestry, nested tools, background completion and isolation", async (t) => {
  const h = await harness(t);
  await h.emit("before_agent_start", { reason: "prompt", prompt: "parent" });
  await h.emit("tool_execution_start", {
    toolCallId: "spawn",
    toolName: "task",
    args: { task: "child" },
  });
  h.bus["task:subagent:lifecycle"]({
    id: "child",
    status: "started",
    parentToolCallId: "spawn",
    agent: "explore",
  });
  h.bus["task:subagent:event"]({
    id: "child",
    event: {
      type: "tool_execution_start",
      toolCallId: "nested-spawn",
      toolName: "task",
    },
  });
  h.bus["task:subagent:lifecycle"]({
    id: "grandchild",
    status: "started",
    parentToolCallId: "nested-spawn",
    agent: "explore",
  });
  h.bus["task:subagent:event"]({
    id: "grandchild",
    event: { type: "message_end", ...assistant("grandchild") },
  });
  h.bus["task:subagent:lifecycle"]({ id: "grandchild", status: "completed" });
  await h.emit("tool_execution_end", {
    toolCallId: "spawn",
    result: { content: [{ type: "text", text: "background job" }] },
  });
  await h.emit("agent_end", { willContinue: true, awaitingAsyncWork: true });
  h.bus["task:subagent:event"]({
    id: "child",
    event: {
      type: "tool_execution_end",
      toolCallId: "nested-spawn",
      result: {},
    },
  });
  h.bus["task:subagent:event"]({
    id: "child",
    event: { type: "message_end", ...assistant("child") },
  });
  h.bus["task:subagent:lifecycle"]({ id: "child", status: "completed" });
  await h.emit("message_end", assistant("parent"));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const spans = h.spans();
  assert.equal(spans.filter((s) => !s.parentSpanId).length, 1);
  assert.equal(new Set(spans.map((s) => s.traceId)).size, 1);
  for (const s of spans.filter((s) => s.parentSpanId))
    assert(spans.some((p) => p.spanId === s.parentSpanId));
  assert.equal(
    spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    3,
  );
});
test("compaction and branch lifecycle; child-bound factories cannot duplicate root exports", async (t) => {
  const h = await harness(t);
  await h.emit("before_agent_start", { reason: "prompt", prompt: "old" });
  await h.emit("agent_start");
  assert.equal(h.handlers.session_before_compact, undefined);
  await h.emit("session_compact");
  h.switchSession("new");
  await h.emit("session_branch");
  h.ctx.agent.kind = "sub";
  await h.emit("before_agent_start", {
    reason: "prompt",
    prompt: "child-local",
  });
  await h.emit("message_end", assistant());
  h.ctx.agent.kind = "main";
  await h.emit("session_shutdown");
  const spans = h.spans();
  assert.equal(spans.length, 2);
  assert(spans.some((s) => attrs(s)["fi.agent.event"] === "compaction"));
  assert(!JSON.stringify(spans).includes("child-local"));
});
test("usage preserves cache and declares host estimate; JSON removes images and headers", () => {
  assert.deepEqual(
    ompUsage({ input: 10, output: 2, cacheRead: 3, cost: { total: 0.02 } }),
    {
      cacheInInput: false,
      reasoningInOutput: true,
      costSource: "host-price-table",
      input: 10,
      output: 2,
      cacheRead: 3,
      costUsd: 0.02,
    },
  );
  assert.equal(ompUsage({ cost: { total: 0 } }), undefined);
  assert(
    !boundedJson({
      headers: { secret: "abc" },
      body: { type: "image", data: "bytes" },
    }).includes("bytes"),
  );
});
test("retained child executions survive parent settlement, new prompts and completed roots", async (t) => {
  const h = await harness(t);
  const start = () =>
    h.bus["task:subagent:lifecycle"]({
      id: "retained",
      status: "started",
      parentToolCallId: "spawn",
      agent: "explore",
    });
  const generation = (text) =>
    h.bus["task:subagent:event"]({
      id: "retained",
      event: { type: "message_end", ...assistant(text) },
    });
  const complete = () =>
    h.bus["task:subagent:lifecycle"]({ id: "retained", status: "completed" });
  await h.emit("before_agent_start", { prompt: "original" });
  await h.emit("tool_execution_start", {
    toolCallId: "spawn",
    toolName: "task",
  });
  await h.emit("tool_execution_end", {
    toolCallId: "spawn",
    result: "background",
  });
  start();
  start(); // a duplicate while pending must not create another scope
  generation("first-wake");
  complete();
  start();
  start();
  await h.emit("agent_end"); // must not finalize the pending second wake
  await h.emit("before_agent_start", { prompt: "subsequent" });
  generation("second-wake");
  complete();
  // A third wake after its original root finalized is linked to its verified spawning tool.
  start();
  generation("third-wake");
  complete();
  await h.emit("message_end", assistant("new-prompt"));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const spans = h.spans(),
    roots = spans.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 3);
  const byOutput = (v) =>
    spans.find(
      (s) =>
        attrs(s)["fi.span.kind"] === "LLM" && attrs(s)["output.value"] === v,
    );
  const first = byOutput("first-wake"),
    second = byOutput("second-wake"),
    third = byOutput("third-wake");
  assert(first && second && third);
  assert.equal(first.traceId, second.traceId);
  assert.notEqual(first.parentSpanId, second.parentSpanId);
  assert.notEqual(second.traceId, byOutput("new-prompt").traceId);
  const agents = spans.filter((s) => s.name === "explore");
  assert.equal(agents.length, 3);
  assert(agents.every((s) => attrs(s)["fi.agent.outcome"] === "success"));
  const spawningTool = spans.find((s) => s.name === "task");
  const continuation = roots.find((s) => s.traceId === third.traceId);
  assert.equal(attrs(continuation)["fi.agent.continuation"], "retained-agent");
  assert(
    continuation.links.some(
      (l) =>
        l.traceId === spawningTool.traceId && l.spanId === spawningTool.spanId,
    ),
  );
  assert.equal(new Set(spans.map((s) => s.spanId)).size, spans.length);
});
for (const override of ["endpoint", "project"]) {
  test(`repository ${override} cannot approve environment credentials or export plugin spans`, async (t) => {
    const h = await harness(t);
    const endpoint = process.env.FI_OTLP_ENDPOINT;
    const project = { schemaVersion: 1, enabled: true };
    if (override === "endpoint") {
      project.transport = { endpoint };
      delete process.env.FI_OTLP_ENDPOINT;
    } else {
      project.projectName = "unapproved-project";
      delete process.env.FI_PROJECT_NAME;
    }
    await fs.mkdir(path.join(h.home, ".omp"));
    await fs.writeFile(
      path.join(h.home, ".omp/futureagi.json"),
      JSON.stringify(project),
    );
    const { resolveConfig } = require("@traceai/agent-core");
    const resolved = await resolveConfig({
      agent: "oh-my-pi",
      projectDir: h.home,
    });
    assert.equal(resolved.destinationApproved, false);
    assert.equal(resolved.enabled, false);
    assert(resolved.issues.includes("DESTINATION_NOT_APPROVED"));
    await h.emit("before_agent_start", { prompt: "private-prompt" });
    await h.emit("message_end", assistant("private-answer"));
    await h.emit("agent_end");
    await h.emit("session_shutdown");
    assert.equal(
      h.batches.length,
      0,
      "no authentication headers or traces may reach repository destination",
    );
  });
}
test("structured and JSON-text tool credentials are redacted end to end", async (t) => {
  const h = await harness(t);
  await h.emit("before_agent_start", { prompt: "public-prompt" });
  await h.emit("tool_execution_start", {
    toolCallId: "credential-tool",
    toolName: "login",
    args: {
      visible: "public-input",
      nested: [
        {
          password: "SYNTH_PASSWORD",
          access_token: "SYNTH_ACCESS",
          refreshToken: "SYNTH_REFRESH",
          clientSecret: "SYNTH_CLIENT",
        },
      ],
    },
  });
  await h.emit("tool_execution_end", {
    toolCallId: "credential-tool",
    result: {
      visible: "public-output",
      token: { private: "SYNTH_OBJECT" },
      nested: [{ password: "SYNTH_OUT_PASSWORD" }],
      content: [
        {
          type: "text",
          text: '{"password":"SYNTH_JSON_PASSWORD", "refresh_token": "SYNTH_JSON_REFRESH", "visible":"public-json"}',
        },
      ],
    },
  });
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const text = JSON.stringify(h.spans());
  assert(!text.includes("SYNTH_"), text);
  assert(
    text.includes("public-input") &&
      text.includes("public-output") &&
      text.includes("public-json"),
  );
});
test("quoted dotenv, shell and config assignments cannot reach exported tool results or context", async (t) => {
  const h = await harness(t);
  const text = [
    'export OPENAI_API_KEY="SYNTH_DOUBLE"',
    "AWS_SECRET_ACCESS_KEY='SYNTH_SINGLE'",
    String.raw`CLIENT_SECRET="SYNTH_ESCAPED\"_TAIL" PUBLIC_SETTING="public-double"`,
    String.raw`AUTH_TOKEN='SYNTH_ESCAPED\'_TAIL' PUBLIC_SETTING='public-single'`,
    'PASSWORD="SYNTH_LINE_ONE\nSYNTH_LINE_TWO"',
    'api_key: "SYNTH_CONFIG"; visible: "public-config"',
    '"client_secret" = "SYNTH_TOML"; "visible" = "public-toml"',
    "OPENAI_API_KEY=SYNTH_UNQUOTED",
    "input_tokens=42 output_tokens=7",
  ].join("\n");
  await h.emit("before_agent_start", { prompt: "public-prompt" });
  await h.emit("tool_execution_start", { toolName: "read", toolCallId: "t" });
  await h.emit("tool_execution_end", {
    toolCallId: "t",
    result: { content: [{ type: "text", text }] },
  });
  await h.emit("message_end", assistant("public-answer"));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const exported = JSON.stringify(h.spans());
  assert(!exported.includes("SYNTH_"), exported);
  assert(!exported.includes("_TAIL"), exported);
  for (const marker of [
    "public-double",
    "public-single",
    "public-config",
    "public-toml",
    "input_tokens=42",
    "output_tokens=7",
    "public-answer",
  ])
    assert(exported.includes(marker), marker);
});
for (const setting of [
  "captureContent",
  "FI_HIDE_INPUTS",
  "FI_HIDE_OUTPUTS",
  "FI_HIDE_ERROR_DETAILS",
]) {
  test(`new prompt adopts ${setting} with pending child and discards the old provider queue`, async (t) => {
    const h = await harness(t);
    await h.emit("before_agent_start", { prompt: "OLD_BUFFERED_PROMPT" });
    await h.emit("tool_execution_start", {
      toolName: "task",
      toolCallId: "spawn",
      args: { text: "OLD_BUFFERED_INPUT" },
    });
    await h.emit("tool_execution_end", {
      toolCallId: "spawn",
      result: "OLD_BUFFERED_OUTPUT",
    });
    h.bus["task:subagent:lifecycle"]({
      id: "pending",
      status: "started",
      parentToolCallId: "spawn",
    });
    await h.emit("agent_end", { awaitingAsyncWork: true });
    if (setting === "captureContent") {
      await fs.mkdir(path.join(h.home, ".omp"));
      await fs.writeFile(
        path.join(h.home, ".omp/futureagi.json"),
        JSON.stringify({ schemaVersion: 1, captureContent: false }),
      );
    } else process.env[setting] = "true";
    await h.emit("before_agent_start", { prompt: "NEXT_PROMPT" });
    await h.emit("agent_start");
    // The old child's completion must neither export buffered content nor attach to the new root.
    h.bus["task:subagent:event"]({
      id: "pending",
      event: { type: "message_end", ...assistant("OLD_BUFFERED_CHILD") },
    });
    h.bus["task:subagent:lifecycle"]({ id: "pending", status: "completed" });
    const next = assistant("NEXT_ANSWER");
    if (setting === "FI_HIDE_ERROR_DETAILS") next.message.stopReason = "error";
    await h.emit("message_end", next);
    await h.emit("agent_end");
    await h.emit("session_shutdown");
    const spans = h.spans(),
      exported = JSON.stringify(spans);
    assert.equal(spans.filter((s) => !s.parentSpanId).length, 1);
    assert.equal(
      spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
      1,
    );
    assert(!exported.includes("OLD_BUFFERED_"), exported);
    assert.equal(
      exported.includes("NEXT_PROMPT"),
      ["FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS"].includes(setting),
    );
    assert.equal(
      exported.includes("NEXT_ANSWER"),
      setting === "FI_HIDE_INPUTS",
    );
  });
}
test("disable during background work discards fenced content and enables the same conversation", async (t) => {
  const h = await harness(t);
  const { disable, enable } = require("@traceai/agent-core");
  await h.emit("before_agent_start", { prompt: "BASELINE" });
  await h.emit("message_end", assistant("baseline-answer"));
  await h.emit("agent_end");
  // Wait for baseline export without closing this host session.
  for (let i = 0; !h.spans().length && i < 100; i++)
    await new Promise((r) => setTimeout(r, 10));
  assert(h.spans().length);
  await h.emit("before_agent_start", { prompt: "FENCED_PROMPT" });
  await h.emit("tool_execution_start", {
    toolName: "task",
    toolCallId: "spawn",
  });
  h.bus["task:subagent:lifecycle"]({
    id: "background",
    status: "started",
    parentToolCallId: "spawn",
  });
  await h.emit("agent_end", { awaitingAsyncWork: true });
  await disable({ agent: "oh-my-pi" });
  h.bus["task:subagent:event"]({
    id: "background",
    event: { type: "message_end", ...assistant("FENCED_ANSWER") },
  });
  h.bus["task:subagent:lifecycle"]({ id: "background", status: "completed" });
  await h.emit("agent_end"); // drains the serialized bus
  await enable({ agent: "oh-my-pi" });
  for (const prompt of ["ENABLED_ONE", "ENABLED_TWO"]) {
    await h.emit("before_agent_start", { prompt });
    await h.emit("message_end", assistant(prompt));
    await h.emit("agent_end");
  }
  await h.emit("session_shutdown");
  assert(!JSON.stringify(h.spans()).includes("FENCED_"));
  const roots = h.spans().filter((s) => !s.parentSpanId);
  assert.deepEqual(
    roots.map((s) => attrs(s)["input.value"]),
    ["BASELINE", "ENABLED_ONE", "ENABLED_TWO"],
  );
  assert.equal(new Set(roots.map((s) => attrs(s)["session.id"])).size, 1);
});

test("namespace credentials and Gemini signatures are removed before OTLP export", async (t) => {
  const h = await harness(t);
  const payload = {
    contents: [
      {
        role: "model",
        parts: [
          {
            text: "ordinary-text",
            thoughtSignature: "SYNTH_THOUGHT",
            thought_signature: "SYNTH_SNAKE",
            thinkingSignature: "SYNTH_THINKING",
          },
        ],
      },
    ],
  };
  const clean = boundedJson(payload);
  assert(clean.includes("ordinary-text"));
  assert(!clean.includes("SYNTH_"));
  await h.emit("before_agent_start", { prompt: "public-prompt" });
  await h.emit("before_provider_request", { payload });
  await h.emit("tool_execution_start", {
    toolCallId: "credential-tool",
    toolName: "read",
    args: {
      visible: "public-input",
      OPENAI_API_KEY: "SYNTH_PROVIDER",
      AWS_SECRET_ACCESS_KEY: "SYNTH_AWS",
      aws_session_token: { value: "SYNTH_SESSION" },
    },
  });
  await h.emit("tool_execution_end", {
    toolCallId: "credential-tool",
    result: {
      payload,
      content: [
        {
          type: "text",
          text: '{"OPENAI_API_KEY":"SYNTH_JSON_PROVIDER","AWS_SECRET_ACCESS_KEY":"SYNTH_JSON_AWS","visible":"public-json","input_tokens":42}',
        },
      ],
    },
  });
  await h.emit("message_end", assistant("ordinary-answer"));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const text = JSON.stringify(h.spans());
  assert(!text.includes("SYNTH_"));
  for (const v of [
    "public-input",
    "public-json",
    "ordinary-text",
    "input_tokens",
    "ordinary-answer",
  ])
    assert(text.includes(v), v);
});

test("unsafe external-drive project config is ignored without losing user tracing", async (t) => {
  const h = await harness(t);
  const drive = path.join(h.home, "external-drive"),
    repo = path.join(drive, "repo");
  await fs.mkdir(path.join(repo, ".omp"), { recursive: true });
  await fs.chmod(drive, 0o775);
  await fs.writeFile(
    path.join(repo, ".omp/futureagi.json"),
    JSON.stringify({ schemaVersion: 1, enabled: false }),
  );
  h.ctx.cwd = repo;
  const { resolveConfig, inspectStatus } = require("@traceai/agent-core");
  const options = { agent: "oh-my-pi", projectDir: repo };
  const config = await resolveConfig(options);
  assert.equal(config.enabled, true);
  assert(config.issues.includes("PROJECT_CONFIG_IGNORED"));
  assert(
    (await inspectStatus(options)).issues.includes("PROJECT_CONFIG_IGNORED"),
  );
  await h.emit("before_agent_start", { prompt: "EXTERNAL_DRIVE" });
  await h.emit("message_end", assistant("drive-answer"));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  assert.equal(h.spans().filter((s) => !s.parentSpanId).length, 1);
});

test("malformed future host event records a visible gap rather than losing the turn", async (t) => {
  const h = await harness(t);
  await h.emit("before_agent_start", { prompt: "future host" });
  await h.emit("message_end", {
    message: { role: "assistant", content: "unknown shape" },
  });
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  assert.equal(h.spans().length, 1);
  assert.equal(attrs(h.spans()[0])["fi.agent.gap"], "UNKNOWN_OMP_EVENT_SHAPE");
});

test("generation context, tool-only output, tool names and usage semantics are visible once", async (t) => {
  const h = await harness(t);
  await h.emit("before_agent_start", { prompt: "CONTEXT_PROMPT" });
  const first = assistant("", 1000);
  first.message.content = [
    {
      type: "toolCall",
      name: "read",
      id: "t",
      arguments: { path: "sample.txt" },
    },
  ];
  first.message.ttft = 25;
  await h.emit("message_end", first);
  await h.emit("tool_execution_start", {
    toolCallId: "t",
    toolName: "read",
    args: {},
  });
  await h.emit("tool_execution_end", {
    toolCallId: "t",
    result: { text: "CONTEXT_RESULT" },
  });
  await h.emit("message_end", assistant("answer", 2000));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const spans = h.spans(),
    models = spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(models.length, 2);
  assert(attrs(models[0])["input.value"].includes("CONTEXT_PROMPT"));
  assert(attrs(models[0])["output.value"].includes("toolCall"));
  assert(attrs(models[1])["input.value"].includes("CONTEXT_RESULT"));
  assert.equal(
    attrs(spans.find((s) => s.name === "read"))["tool.name"],
    "read",
  );
  assert.equal(
    spans.reduce(
      (sum, s) => sum + Number(attrs(s)["gen_ai.usage.total_tokens"] ?? 0),
      0,
    ),
    34,
  );
  assert.equal(
    spans.reduce(
      (sum, s) => sum + Number(attrs(s)["gen_ai.cost.total"] ?? 0),
      0,
    ),
    0.02,
  );
  assert.equal(
    attrs(models[0])["gen_ai.server.time_to_first_token"],
    undefined, // Reconstructed generations cannot claim native measured request timing.
  );
});

test("hidden errors cannot reappear through subsequent reconstructed context", async (t) => {
  const h = await harness(t);
  process.env.FI_HIDE_ERROR_DETAILS = "true";
  await h.emit("before_agent_start", { prompt: "PUBLIC_PROMPT" });
  const failed = assistant("PRIVATE_MODEL_ERROR", 1000);
  failed.message.stopReason = "error";
  await h.emit("message_end", failed);
  await h.emit("tool_execution_start", { toolName: "read", toolCallId: "t" });
  await h.emit("tool_execution_end", {
    toolCallId: "t",
    isError: true,
    result: { text: "PRIVATE_TOOL_ERROR" },
  });
  await h.emit("message_end", assistant("PUBLIC_RECOVERY", 2000));
  await h.emit("agent_end");
  await h.emit("session_shutdown");
  const text = JSON.stringify(h.spans());
  assert(!text.includes("PRIVATE_"));
  assert(text.includes("PUBLIC_RECOVERY"));
  assert.equal(
    h.spans().filter((s) => attrs(s)["fi.agent.outcome"] === "error").length,
    2,
  );
});

for (const setting of [
  "FI_AGENT_CAPTURE_CONTENT",
  "FI_HIDE_INPUTS",
  "FI_HIDE_OUTPUTS",
]) {
  test(`tool content still obeys ${setting}`, async (t) => {
    const h = await harness(t);
    process.env[setting] =
      setting === "FI_AGENT_CAPTURE_CONTENT" ? "false" : "true";
    await h.emit("before_agent_start", { prompt: "prompt" });
    await h.emit("tool_execution_start", {
      toolCallId: "t",
      toolName: "read",
      args: { text: "INPUT_MARKER" },
    });
    await h.emit("tool_execution_end", {
      toolCallId: "t",
      result: { text: "OUTPUT_MARKER" },
    });
    await h.emit("agent_end");
    await h.emit("session_shutdown");
    const text = JSON.stringify(h.spans());
    assert.equal(text.includes("INPUT_MARKER"), setting === "FI_HIDE_OUTPUTS");
    assert.equal(text.includes("OUTPUT_MARKER"), setting === "FI_HIDE_INPUTS");
    assert.equal(h.handlers.session_before_compact, undefined);
  });
}

test("a project-config FIFO fails open without blocking subsequent prompt handlers", async (t) => {
  const { execFile } = require("node:child_process");
  const { promisify } = require("node:util");
  const h = await harness(t);
  await fs.mkdir(path.join(h.home, ".omp"));
  await promisify(execFile)("mkfifo", [path.join(h.home, ".omp/futureagi.json")]);
  const deadline = setTimeout(() => { throw new Error("FIFO_BLOCKED_EVENT_QUEUE"); }, 3000);
  try {
    await h.emit("session_start");
    await h.emit("before_agent_start", { reason: "prompt", prompt: "FIFO_FALLBACK" });
    await h.emit("before_provider_request", { payload: { messages: [{ role: "user", content: "FIFO_FALLBACK" }] } });
    await h.emit("message_end", assistant("fifo survived"));
    await h.emit("agent_end");
    await h.emit("session_shutdown");
    const core = require("@traceai/agent-core");
    assert((await core.resolveConfig({ agent: "oh-my-pi", projectDir: h.home })).issues.includes("PROJECT_CONFIG_IGNORED"));
    assert.equal(h.spans().filter((s) => !s.parentSpanId).length, 1);
  } finally { clearTimeout(deadline); }
});
