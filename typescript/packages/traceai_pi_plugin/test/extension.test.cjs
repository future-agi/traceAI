const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const c = require("@traceai/agent-core");
const plugin = require("../dist/index.js");
const { SUBAGENT_CHANNEL } = require("../dist/subagent.js");
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
async function fixture(t, options = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "pi-test-"));
  const batches = [];
  const server = http.createServer((req, res) => {
    let data = "";
    req.on("data", (b) => (data += b));
    req.on("end", () => {
      batches.push(JSON.parse(data));
      res.setHeader("content-type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const env = {
    FI_AGENT_CONFIG_DIR: path.join(home, "config"),
    PI_CODING_AGENT_DIR: path.join(home, "agent"),
    XDG_STATE_HOME: path.join(home, "state"),
    ...(options.env ?? {}),
  };
  const old = {};
  for (const [k, v] of Object.entries(env)) {
    old[k] = process.env[k];
    process.env[k] = v;
  }
  await c.setup({
    agent: "pi",
    env,
    projectDir: home,
    credentials: { apiKey: "unit-api-key", secretKey: "unit-secret-key" },
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    projectName: "pi",
    configureOnly: true,
    captureContent: options.capture,
  });
  const handlers = {},
    bus = {},
    commands = {},
    ui = [];
  let entries = [
    {
      id: "user-1",
      type: "message",
      message: { role: "user", content: "first prompt" },
    },
  ];
  const ctx = {
    cwd: home,
    mode: options.mode ?? "json",
    hasUI: options.hasUI ?? false,
    signal: new AbortController().signal,
    model: { id: "test", provider: "mock" },
    sessionManager: {
      getBranch: () => entries,
      getSessionId: () => "stable-session",
    },
    ui: { setStatus: (...a) => ui.push(a), notify: (...a) => ui.push(a) },
  };
  const pi = {
    on: (n, h) => ((handlers[n] = h), () => delete handlers[n]),
    registerCommand: (n, h) => (commands[n] = h),
    events: {
      emit: (n, data) => bus[n]?.(data),
      on: (n, h) => ((bus[n] = h), () => delete bus[n]),
    },
  };
  plugin.default(pi);
  const emit = async (type, data = {}) => {
    assert(handlers[type], type);
    assert.equal(await handlers[type]({ type, ...data }, ctx), undefined);
  };
  await emit("session_start");
  t.after(async () => {
    await emit("session_shutdown");
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    for (const k of Object.keys(env))
      old[k] === undefined ? delete process.env[k] : (process.env[k] = old[k]);
    await fs.rm(home, { recursive: true, force: true });
  });
  return {
    ctx,
    ui,
    bus,
    commands,
    emit,
    env,
    home,
    entries,
    spans: () =>
      batches.flatMap((b) =>
        b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
      ),
  };
}
async function request(
  f,
  text = "answer",
  usage = {
    input: 10,
    cacheRead: 3,
    cacheWrite: 2,
    output: 4,
    reasoning: 1,
    cost: { total: 0.01 },
  },
) {
  await f.emit("before_provider_request", {
    payload: {
      messages: [{ role: "user", content: "first prompt unit-secret-key" }],
      authorization: "NEVER",
    },
  });
  await f.emit("message_update", {
    assistantMessageEvent: { type: "text_delta" },
  });
  await f.emit("message_end", {
    message: {
      role: "assistant",
      content: [{ type: "text", text }],
      stopReason: "stop",
      usage,
    },
  });
}
test("two prompts are two roots, request usage is additive exactly once, native content privacy and UI gating", async (t) => {
  const f = await fixture(t);
  await f.emit("before_agent_start");
  await request(f);
  await f.emit("tool_execution_start", {
    toolCallId: "a",
    toolName: "read",
    args: { path: "file" },
  });
  await f.emit("tool_execution_end", {
    toolCallId: "a",
    isError: false,
    result: { content: [{ type: "text", text: "file body" }] },
  });
  await request(f, "done");
  await f.emit("agent_settled", { aborted: false });
  f.entries.push({
    id: "user-2",
    type: "message",
    message: { role: "user", content: "second" },
  });
  await f.emit("before_agent_start");
  await request(f);
  await f.emit("agent_settled", { aborted: false });
  await f.emit("session_shutdown");
  const spans = f.spans(),
    roots = spans.filter((s) => !s.parentSpanId),
    llm = spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(roots.length, 2);
  assert.equal(llm.length, 3);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.equal(attrs(roots[0])["session.id"], attrs(roots[1])["session.id"]);
  assert.equal(attrs(llm[0])["gen_ai.usage.input_tokens"], 15);
  assert.equal(attrs(llm[0])["gen_ai.usage.output_tokens"], 4);
  assert.equal(attrs(llm[0])["fi.agent.cost.source"], "host-price-table");
  assert.equal(attrs(llm[0])["gen_ai.cost.total"], 0.01);
  assert.equal(attrs(llm[0])["gen_ai.usage.cost"], undefined);
  assert(attrs(llm[0])["gen_ai.server.time_to_first_token"] >= 0);
  assert.equal(
    attrs(spans.find((s) => s.name === "read"))["tool.name"],
    "read",
  );
  assert.equal(attrs(roots[0])["gen_ai.usage.input_tokens"], undefined);
  assert(!JSON.stringify(spans).includes("unit-secret-key"));
  assert(!JSON.stringify(spans).includes("NEVER"));
  assert.equal(f.ui.length, 0);
  await f.commands.futureagi.handler("", f.ctx);
  assert.equal(f.ui.length, 0);
});
test("abort, tool failure, active-turn compaction and explicit subagent boundary", async (t) => {
  const f = await fixture(t);
  await request(f);
  await f.emit("tool_execution_start", {
    toolCallId: "spawn",
    toolName: "subagent",
    args: { task: "inspect" },
  });
  const emit = (e) => f.bus[SUBAGENT_CHANNEL](e);
  emit({ type: "start", id: "child", parentToolCallId: "not-running" });
  emit({
    type: "start",
    id: "child",
    parentToolCallId: "spawn",
    name: "child",
  });
  emit({
    type: "request",
    id: "req",
    parentId: "child",
    model: "test",
    provider: "mock",
    input: "child prompt",
  });
  emit({
    type: "end",
    id: "req",
    result: {
      output: "child answer",
      usage: {
        input: 5,
        output: 2,
        cacheInInput: false,
        reasoningInOutput: true,
      },
    },
  });
  emit({ type: "end", id: "child" });
  await f.emit("tool_execution_end", {
    toolCallId: "spawn",
    isError: true,
    result: { content: [{ type: "text", text: "tool failure" }] },
  });
  await f.emit("session_before_compact");
  await f.emit("session_compact", { compactionEntry: { summary: "summary" } });
  await f.emit("before_provider_request", { payload: { messages: [] } });
  await f.emit("message_end", {
    message: { role: "assistant", stopReason: "aborted", content: [] },
  });
  await f.emit("agent_settled", { aborted: true });
  await f.emit("session_shutdown");
  const spans = f.spans();
  assert.equal(spans.filter((s) => !s.parentSpanId).length, 1);
  const root = spans.find((s) => !s.parentSpanId),
    tool = spans.find((s) => s.name === "subagent"),
    child = spans.find((s) => s.name === "child"),
    childLLM = spans.find((s) => s.name === "llm");
  assert.equal(attrs(root)["fi.agent.outcome"], "interrupted");
  assert.equal(tool.status.code, 2);
  assert.equal(child.parentSpanId, tool.spanId);
  assert.equal(childLLM.parentSpanId, child.spanId);
  assert(spans.some((s) => attrs(s)["fi.agent.event"] === "compaction"));
});
test("metadata-only drops every content field and existing stricter switches win", async (t) => {
  const f = await fixture(t, { capture: false });
  await request(f, "PRIVATE");
  await f.emit("tool_execution_start", {
    toolCallId: "a",
    toolName: "read",
    args: { secret: "PRIVATE" },
  });
  await f.emit("tool_execution_end", {
    toolCallId: "a",
    isError: true,
    result: "PRIVATE",
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  assert(!JSON.stringify(f.spans()).includes("PRIVATE"));
  assert(
    f
      .spans()
      .every(
        (s) =>
          attrs(s)["input.value"] === undefined &&
          attrs(s)["output.value"] === undefined,
      ),
  );
});
test("persistent disable fences an already-running extension and queued spans", async (t) => {
  const f = await fixture(t);
  await f.emit("before_provider_request", { payload: { messages: [] } });
  await c.disable({ agent: "pi", env: f.env });
  await f.emit("message_end", {
    message: {
      role: "assistant",
      content: [{ type: "text", text: "never" }],
      stopReason: "stop",
    },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  assert.equal(f.spans().length, 0);
});
test("RPC hasUI does not emit interactive status UI", async (t) => {
  const f = await fixture(t, { mode: "rpc", hasUI: true });
  await f.commands.futureagi.handler("", f.ctx);
  assert.equal(f.ui.length, 0);
});
test("interactive status is local, and bounds scrub images and provider header fields", async (t) => {
  const f = await fixture(t, { mode: "tui", hasUI: true });
  await f.commands.futureagi.handler("", f.ctx);
  assert(f.ui.length >= 2);
  assert(!JSON.stringify(f.ui).includes("unit-api-key"));
  const text = plugin.boundedJson({
    headers: { authorization: "SECRET" },
    content: [
      { type: "image_url", image_url: { url: "data:SECRET" } },
      { type: "text", text: "x".repeat(50000) },
    ],
  });
  assert(text.length <= 20000);
  assert(!text.includes("SECRET"));
});
test("hide-inputs strips tool-call arguments from assistant output too", async (t) => {
  const f = await fixture(t, { env: { FI_HIDE_INPUTS: "true" } });
  await f.emit("before_provider_request", {
    payload: { messages: [{ role: "user", content: "PRIVATE_INPUT" }] },
  });
  await f.emit("message_end", {
    message: {
      role: "assistant",
      stopReason: "toolUse",
      content: [
        {
          type: "toolCall",
          id: "call",
          name: "read",
          arguments: { path: "PRIVATE_INPUT" },
        },
        { type: "text", text: "Visible answer" },
      ],
    },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  assert(!JSON.stringify(f.spans()).includes("PRIVATE_INPUT"));
  assert(JSON.stringify(f.spans()).includes("Visible answer"));
});
test("repository transport cannot use environment credentials without independent approval", async (t) => {
  const f = await fixture(t, {
    env: { FI_API_KEY: "synthetic-api", FI_SECRET_KEY: "synthetic-secret" },
  });
  const received = [];
  const receiver = http.createServer((req, res) => {
    received.push(req.headers);
    req.resume();
    res.end("{}");
  });
  await new Promise((r) => receiver.listen(0, "127.0.0.1", r));
  t.after(async () => {
    receiver.closeAllConnections();
    await new Promise((r) => receiver.close(r));
  });
  await fs.mkdir(path.join(f.home, ".pi"), { mode: 0o700 });
  await fs.writeFile(
    path.join(f.home, ".pi/futureagi.json"),
    JSON.stringify({
      schemaVersion: 1,
      transport: {
        endpoint: `http://127.0.0.1:${receiver.address().port}/tracer/v1/traces`,
      },
    }),
    { mode: 0o600 },
  );
  await f.emit("before_agent_start");
  await request(f, "SYNTHETIC_CONVERSATION");
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  assert.equal(
    received.length,
    0,
    "no headers or content may reach the unapproved receiver",
  );
  assert.equal(f.spans().length, 0);
});
test("synthetic credentials in structured arguments, JSON output and escaped provider text never export", async (t) => {
  const f = await fixture(t);
  await request(f, 'prefix {\\"api_key\\":\\"UNRELATED_API_VALUE\\"} suffix');
  await f.emit("tool_execution_start", {
    toolCallId: "cred",
    toolName: "request",
    args: {
      password: "PASSWORD_VALUE",
      token: "TOKEN_VALUE",
      nested: { client_secret: "CLIENT_SECRET_VALUE" },
      safe: "kept",
    },
  });
  await f.emit("tool_execution_end", {
    toolCallId: "cred",
    isError: false,
    result: {
      content: [
        {
          type: "text",
          text: JSON.stringify({ api_key: "UNRELATED_API_VALUE" }),
        },
      ],
    },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  const serialized = JSON.stringify(f.spans());
  for (const value of [
    "PASSWORD_VALUE",
    "TOKEN_VALUE",
    "CLIENT_SECRET_VALUE",
    "UNRELATED_API_VALUE",
  ])
    assert(!serialized.includes(value), value);
  assert(serialized.includes("kept"));
  for (const text of [
    '{"password":"VALUE"}',
    'text {\\"token\\":\\"VALUE\\"}',
    JSON.stringify(JSON.stringify({ access_token: "VALUE" })),
  ])
    assert(!c.redact(text).includes("VALUE"));
  assert(!c.redact(String.raw`prefix {"password":"VALUE"}`).includes("VALUE"));
  assert(
    !c
      .redact(String.raw`prefix {\"access_token\":\"VALUE\"}`)
      .includes("VALUE"),
  );
  assert(
    !plugin
      .boundedJson({ aws_secret_access_key: "VALUE", clientSecret: "VALUE" })
      .includes("VALUE"),
  );
  assert.equal(c.redact('{"input_tokens":12}'), '{"input_tokens":12}');
});
test("physical generation identity replaces the virtual selection", async (t) => {
  const f = await fixture(t);
  f.ctx.model = { id: "auto", provider: "router" };
  await f.emit("before_provider_request", { payload: { messages: [] } });
  await f.emit("message_end", {
    message: {
      role: "assistant",
      content: [],
      model: "physical",
      provider: "actual",
      stopReason: "stop",
    },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  const llm = f.spans().find((s) => s.name === "pi.generation");
  assert.equal(attrs(llm)["gen_ai.request.model"], "physical");
  assert.equal(attrs(llm)["gen_ai.provider.name"], "actual");
});
for (const flag of ["FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS"])
  test(`${flag} also protects replayed provider history across turns`, async (t) => {
    const f = await fixture(t, { env: { [flag]: "true" } });
    await request(f);
    await f.emit("tool_execution_end", {
      toolCallId: "failed",
      isError: true,
      result: { content: [{ type: "text", text: "HIDDEN_HISTORY" }] },
    });
    await f.emit("agent_settled");
    f.entries.push({
      id: "user-2",
      type: "message",
      message: { role: "user", content: "second" },
    });
    await f.emit("before_agent_start");
    await f.emit("before_provider_request", {
      payload: {
        messages: [
          { role: "tool", tool_call_id: "failed", content: "HIDDEN_HISTORY" },
        ],
      },
    });
    await f.emit("message_end", {
      message: { role: "assistant", content: [], stopReason: "stop" },
    });
    await f.emit("agent_settled");
    await f.emit("session_shutdown");
    assert(!JSON.stringify(f.spans()).includes("HIDDEN_HISTORY"));
  });
test("ownership uses the shared bus across distinct wrappers and releases on shutdown/reload", async () => {
  const bus = new (require("node:events").EventEmitter)();
  function host() {
    const handlers = {};
    return {
      handlers,
      on: (n, h) => {
        handlers[n] = h;
      },
      registerCommand() {},
      events: {
        emit: (n, d) => bus.emit(n, d),
        on: (n, h) => {
          bus.on(n, h);
          return () => bus.off(n, h);
        },
      },
    };
  }
  const first = host(),
    second = host();
  plugin.default(first);
  plugin.default(second);
  assert(first.handlers.session_start);
  assert.equal(Object.keys(second.handlers).length, 0);
  await first.handlers.session_shutdown({}, {});
  const reloaded = host();
  plugin.default(reloaded);
  assert(reloaded.handlers.session_start);
  await reloaded.handlers.session_shutdown({}, {});
  assert.equal(bus.eventNames().length, 0);
});
test("Google request capture selects prompt fields without transport, image or credential data", async (t) => {
  const f = await fixture(t);
  await f.emit("before_provider_request", {
    payload: {
      model: "gemini-test",
      contents: [
        {
          role: "user",
          parts: [
            { text: "GOOGLE_PROMPT" },
            { inlineData: { mimeType: "image/png", data: "BINARY_CANARY" } },
            { fileData: { fileUri: "IMAGE_URI_CANARY" } },
          ],
        },
      ],
      config: {
        systemInstruction: "GOOGLE_SYSTEM",
        tools: [
          {
            functionDeclarations: [
              { name: "read", description: "GOOGLE_TOOL" },
            ],
          },
        ],
        httpOptions: { headers: { authorization: "TRANSPORT_CANARY" } },
        apiKey: "GOOGLE_SECRET_CANARY",
      },
    },
  });
  await f.emit("message_end", {
    message: {
      role: "assistant",
      content: [{ type: "text", text: "answer" }],
      stopReason: "stop",
    },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  const input = attrs(f.spans().find((s) => s.name === "pi.generation"))[
    "input.value"
  ];
  for (const marker of ["GOOGLE_PROMPT", "GOOGLE_SYSTEM", "GOOGLE_TOOL"])
    assert(input.includes(marker));
  for (const marker of [
    "BINARY_CANARY",
    "IMAGE_URI_CANARY",
    "TRANSPORT_CANARY",
    "GOOGLE_SECRET_CANARY",
  ])
    assert(!input.includes(marker));
});
for (const flag of [
  "FI_HIDE_INPUTS",
  "FI_HIDE_OUTPUTS",
  "FI_HIDE_ERROR_DETAILS",
  "FI_AGENT_CAPTURE_CONTENT",
])
  test(`Google mixed provider input honors ${flag}`, async (t) => {
    const f = await fixture(t, {
      env: { [flag]: flag === "FI_AGENT_CAPTURE_CONTENT" ? "false" : "true" },
    });
    if (flag === "FI_HIDE_ERROR_DETAILS")
      await f.emit("tool_execution_end", {
        toolCallId: "failed",
        isError: true,
        result: "GOOGLE_PRIVATE",
      });
    await f.emit("before_provider_request", {
      payload: {
        contents: [{ role: "user", parts: [{ text: "GOOGLE_PRIVATE" }] }],
        config: { systemInstruction: "GOOGLE_PRIVATE", tools: [] },
      },
    });
    await f.emit("message_end", {
      message: { role: "assistant", content: [], stopReason: "stop" },
    });
    await f.emit("agent_settled");
    await f.emit("session_shutdown");
    assert(!JSON.stringify(f.spans()).includes("GOOGLE_PRIVATE"));
  });
test("unknown provider shape records a gap without dropping the turn or request", async (t) => {
  const f = await fixture(t);
  await f.emit("before_provider_request", {
    payload: { futureShape: "UNRECOGNIZED_PRIVATE_DATA" },
  });
  await f.emit("message_end", {
    message: { role: "assistant", content: [], stopReason: "stop" },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  assert.equal(f.spans().filter((s) => !s.parentSpanId).length, 1);
  const generation = f.spans().find((s) => s.name === "pi.generation");
  assert.equal(attrs(generation)["fi.agent.gap"], "UNKNOWN_PROVIDER_PAYLOAD");
  assert.equal(attrs(generation)["input.value"], undefined);
});
test("user configuration keeps tracing on a group-writable external project", async (t) => {
  const f = await fixture(t);
  const project = path.join(f.home, "external-volume");
  await fs.mkdir(project, { mode: 0o775 });
  await fs.chmod(project, 0o775);
  f.ctx.cwd = project;
  await f.emit("before_agent_start");
  await request(f);
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  assert.equal(f.spans().filter((s) => !s.parentSpanId).length, 1);
  const status = await c.inspectStatus({ agent: "pi", projectDir: project });
  assert(status.issues.includes("PROJECT_CONFIG_IGNORED"));
});
test("unknown and unpriced usage remain absent", () => {
  assert.equal(plugin.piUsage(undefined), undefined);
  assert.equal(plugin.piUsage({ cost: { total: 0 } }), undefined);
  assert.equal(plugin.piUsage({ input: NaN }), undefined);
});
for (const env of [
  {},
  { FI_HIDE_ERROR_DETAILS: "true" },
  { FI_AGENT_CAPTURE_CONTENT: "false" },
])
  test(`pre-provider assistant failure keeps one error root, no invented generation: ${JSON.stringify(env)}`, async (t) => {
    const f = await fixture(t, { env });
    await f.emit("before_agent_start");
    await f.emit("message_end", {
      message: {
        role: "assistant",
        stopReason: "error",
        errorMessage: "ROUTE_FAILURE_CANARY",
        content: [],
        usage: { input: 0, output: 0 },
      },
    });
    await f.emit("agent_settled", { aborted: false });
    await f.emit("agent_settled", { aborted: false });
    await f.emit("session_shutdown");
    assert.equal(f.spans().length, 1);
    const root = f.spans()[0];
    assert.equal(root.parentSpanId, undefined);
    assert.equal(root.status.code, 2);
    assert.equal(attrs(root)["fi.agent.outcome"], "error");
    assert.equal(attrs(root)["gen_ai.usage.input_tokens"], undefined);
    assert.equal(attrs(root)["gen_ai.cost.total"], undefined);
    assert.equal(
      attrs(root)["input.value"],
      env.FI_AGENT_CAPTURE_CONTENT ? undefined : "first prompt",
    );
    if (Object.keys(env).length)
      assert(!JSON.stringify(root).includes("ROUTE_FAILURE_CANARY"));
    else assert.equal(attrs(root)["output.value"], "ROUTE_FAILURE_CANARY");
  });
test("credential aliases in unquoted, quoted and escaped text are blocked across every content surface", async (t) => {
  const f = await fixture(t);
  const text =
    'AWS_ACCESS_KEY_ID=ACCESS_CANARY\nAWS_SECRET_ACCESS_KEY: SECRET_CANARY\nAuthorization: Basic QkFTSUNfQ0FOQVJZ\nx-api-key=KEY_CANARY\nclientSecret: "MULTILINE_CANARY\ncontinued"';
  for (const rendered of [
    text,
    JSON.stringify(text),
    JSON.stringify({ body: text }),
    JSON.stringify(JSON.stringify({ body: text })),
  ]) {
    assert(!c.redact(rendered).includes("CANARY"));
    assert(!c.redact(rendered).includes("QkFTSUNfQ0FOQVJZ"));
  }
  f.entries[0].message.content = text;
  await f.emit("before_provider_request", {
    payload: {
      messages: [
        { role: "user", content: text },
        { role: "tool", content: text },
      ],
    },
  });
  await f.emit("message_end", {
    message: { role: "assistant", content: [], stopReason: "toolUse" },
  });
  await f.emit("tool_execution_start", {
    toolCallId: "secrets",
    toolName: "read",
    args: {},
  });
  await f.emit("tool_execution_end", {
    toolCallId: "secrets",
    result: { content: [{ type: "text", text }] },
  });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  const serialized = JSON.stringify(f.spans());
  assert.equal(f.spans().length, 3);
  assert(!serialized.includes("CANARY"));
  assert(!serialized.includes("QkFTSUNfQ0FOQVJZ"));
  for (const safe of [
    "input_tokens=12 output_tokens: 4",
    "token_count=3",
    "normal: text",
  ])
    assert.equal(c.redact(safe), safe);
});

test("coalesced delivered prompts keep separate linked roots and one usage owner", async (t) => {
  const f = await fixture(t);
  await request(f);
  f.entries.push(
    { id: "queued-2", type: "message", message: { role: "user", content: "second queued prompt" } },
    { id: "queued-3", type: "message", message: { role: "user", content: "third queued prompt" } },
  );
  await request(f, "shared answer");
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  const spans = f.spans(), roots = spans.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 3);
  assert.equal(spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 2);
  const second = roots.find((s) => attrs(s)["input.value"] === "second queued prompt");
  const third = roots.find((s) => attrs(s)["input.value"] === "third queued prompt");
  assert.equal(second.links[0].traceId, third.traceId);
  assert.equal(second.links[0].spanId, third.spanId);
  assert(roots.every((s) => attrs(s)["gen_ai.usage.total_tokens"] === undefined));
});

test("aggregate compaction usage is additive once without invented generations", async (t) => {
  const f = await fixture(t);
  await request(f);
  await f.emit("session_before_compact");
  await f.emit("session_compact", { compactionEntry: { summary: "summary", usage: { input: 70, cacheRead: 10, output: 20, cost: { total: 0.000111 } } } });
  await f.emit("agent_settled");
  await f.emit("session_shutdown");
  const spans = f.spans(), compact = spans.find((s) => s.name === "pi.compaction");
  assert.equal(attrs(compact)["fi.span.kind"], "AGENT");
  assert.equal(attrs(compact)["gen_ai.usage.input_tokens"], 80);
  assert.equal(attrs(compact)["gen_ai.usage.output_tokens"], 20);
  assert.equal(attrs(compact)["gen_ai.cost.total"], 0.000111);
  assert.equal(attrs(compact)["fi.agent.cost.source"], "host-price-table");
  assert.equal(attrs(compact)["fi.usage.contribution"], "additive");
  assert.equal(spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 1);
  assert.equal(attrs(spans.find((s) => !s.parentSpanId))["gen_ai.usage.total_tokens"], undefined);
});
