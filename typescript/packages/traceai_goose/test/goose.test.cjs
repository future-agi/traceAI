const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { DatabaseSync } = require("node:sqlite");
const core = require("@traceai/agent-core");
const { goosePaths, nativeConflict } = require("../dist/paths.js");
const {
  exportTurn,
  readRows,
  validSession,
  messageIndex,
  persistedPrompt,
} = require("../dist/transcript.js");
const attrs = (list) =>
  Object.fromEntries(
    (list || []).map((a) => [a.key, Object.values(a.value)[0]]),
  );

test("Goose path root and conflict checks preserve native destinations", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "goose-config-"));
  const env = { GOOSE_PATH_ROOT: root };
  assert.equal(goosePaths(env).config, path.join(root, "config"));
  assert.throws(() => goosePaths({ GOOSE_PATH_ROOT: "relative" }));
  assert.equal(
    await nativeConflict({
      ...env,
      OTEL_EXPORTER_OTLP_ENDPOINT: "https://example.invalid",
    }),
    true,
  );
  assert.equal(
    await nativeConflict({
      ...env,
      OTEL_TRACES_EXPORTER: "none",
      OTEL_EXPORTER_OTLP_ENDPOINT: "https://example.invalid",
    }),
    false,
  );
  await fs.mkdir(path.join(root, "config"), { mode: 0o700 });
  await fs.writeFile(
    path.join(root, "config", "config.yaml"),
    "otel_exporter_otlp_endpoint: https://example.invalid\n",
    { mode: 0o600 },
  );
  assert.equal(await nativeConflict(env), true);
  await fs.rm(root, { recursive: true });
});

test("session identifiers cannot select arbitrary files; SQLite turn reads are bounded", () => {
  assert.equal(validSession("../../credentials"), false);
  assert.equal(validSession("20261106_1"), true);
  const db = new DatabaseSync(":memory:");
  db.exec(
    "CREATE TABLE messages(id INTEGER, session_id TEXT, message_id TEXT, role TEXT, content_json TEXT, metadata_json TEXT, created_timestamp INTEGER)",
  );
  db.prepare("INSERT INTO messages VALUES(1,?,'msg-1',?,?,?,?)").run(
    "s",
    "user",
    "[]",
    "{}",
    1,
  );
  assert.equal(readRows(db, "s").length, 1);
  db.prepare("INSERT INTO messages VALUES(2,?,'msg-2',?,?,?,?)").run(
    "s",
    "user",
    "x".repeat(4 * 1024 * 1024 + 1),
    "{}",
    2,
  );
  assert.throws(() => readRows(db, "s"), /TRANSCRIPT_TURN_LIMIT/);
  db.close();
});

test("saved response records: one billable owner, tool ancestry, privacy and estimated costs", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "goose-trace-"));
  const received = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (b) => chunks.push(b));
    req.on("end", () => {
      received.push(JSON.parse(Buffer.concat(chunks)));
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    const env = {
      FI_API_KEY: "dummy-api",
      FI_SECRET_KEY: "dummy-secret",
      FI_AGENT_ENABLED: "true",
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/traces`,
      GOOSE_PATH_ROOT: path.join(root, "goose"),
      FI_AGENT_CONFIG_DIR: path.join(root, "config"),
      XDG_STATE_HOME: path.join(root, "state"),
    };
    const config = await core.resolveConfig({
      agent: "goose",
      home: root,
      env,
    });
    const row = (id, role, parts, meta) => ({
      id,
      message_id: `message-${id}`,
      role,
      content_json: JSON.stringify(parts),
      metadata_json: JSON.stringify(meta),
      created_timestamp: Math.floor(Date.now() / 1000),
    });
    const source = [
      row(
        1,
        "user",
        [{ type: "text", text: "synthetic prompt dummy-secret" }],
        { userVisible: true },
      ),
      row(
        2,
        "assistant",
        [
          {
            type: "toolRequest",
            id: "call-1",
            toolCall: {
              status: "success",
              value: {
                name: "shell",
                arguments: { command: "printf synthetic" },
              },
            },
          },
        ],
        {
          inference: { provider: "openai", requestedModel: "gpt-4o" },
          usage: {
            inputTokens: 100,
            outputTokens: 10,
            cost: 0.01,
            costSource: "estimated",
          },
        },
      ),
      row(
        3,
        "user",
        [
          {
            type: "toolResponse",
            id: "call-1",
            toolResult: {
              status: "success",
              value: {
                isError: true,
                content: [{ type: "text", text: "synthetic tool error" }],
              },
            },
          },
        ],
        { userVisible: true },
      ),
      row(4, "assistant", [{ type: "text", text: "synthetic answer" }], {
        inference: { provider: "openai", requestedModel: "gpt-4o" },
        usage: {
          inputTokens: 120,
          outputTokens: 12,
          cost: 0.02,
          costSource: "provider_reported",
        },
      }),
    ];
    const state = {
      schemaVersion: 2,
      baseline: [],
      completed: [],
      turnId: "turn-1",
      stopped: true,
      pending: true,
      start: Date.now() - 500,
      tools: {},
      generation: 0,
      policy: "test",
    };
    await exportTurn(config, "session-1", state, source, "Stop");
    const spans = received.flatMap((p) =>
      p.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
    assert.equal(spans.length, 4);
    const llms = spans.filter(
      (s) => attrs(s.attributes)["fi.span.kind"] === "LLM",
    );
    assert.equal(llms.length, 2);
    assert.equal(
      llms.reduce(
        (n, s) => n + Number(attrs(s.attributes)["gen_ai.usage.input_tokens"]),
        0,
      ),
      220,
    );
    assert.equal(
      attrs(llms[0].attributes)["fi.agent.cost.source"],
      "estimated",
    );
    assert.equal(attrs(llms[1].attributes)["fi.agent.cost.source"], "reported");
    assert.equal(
      attrs(llms[0].attributes)["fi.agent.fidelity"],
      "recorded-request",
    );
    const tool = spans.find(
      (s) => attrs(s.attributes)["fi.span.kind"] === "TOOL",
    );
    assert.equal(tool.parentSpanId, llms[0].spanId);
    assert.equal(tool.status.code, 2);
    const turn = spans.find((s) => !s.parentSpanId);
    assert.equal(turn.status.code, 2);
    assert.ok(!("gen_ai.usage.input_tokens" in attrs(turn.attributes)));
    const json = JSON.stringify(received);
    assert.ok(!json.includes("dummy-secret"));
    assert.ok(!json.includes("host.name"));
    assert.ok(!json.includes("session.user"));
    assert.ok(json.includes("synthetic prompt"));
    assert.ok(spans.every((s) => !s.events?.length));
    for (const toolResult of [
      { status: "error", error: "HIDDEN_ERROR_SENTINEL" },
      {
        status: "success",
        value: {
          isError: true,
          content: [{ type: "text", text: "HIDDEN_ERROR_SENTINEL" }],
        },
      },
    ]) {
      received.length = 0;
      const privateConfig = await core.resolveConfig({
        agent: "goose",
        home: root,
        env: { ...env, FI_HIDE_ERROR_DETAILS: "true" },
      });
      const privateSource = structuredClone(source);
      privateSource[2].content_json = JSON.stringify([
        { type: "toolResponse", id: "call-1", toolResult },
      ]);
      await exportTurn(privateConfig, "private-errors", state, privateSource);
      assert.ok(received.length);
      assert.ok(!JSON.stringify(received).includes("HIDDEN_ERROR_SENTINEL"));
    }
    for (const key of [
      "FI_HIDE_OUTPUTS",
      "FI_HIDE_OUTPUT_TEXT",
      "FI_HIDE_OUTPUT_MESSAGES",
    ]) {
      received.length = 0;
      const privateConfig = await core.resolveConfig({
        agent: "goose",
        home: root,
        env: { ...env, [key]: "true" },
      });
      const privateSource = structuredClone(source);
      privateSource[2].content_json = JSON.stringify([
        {
          type: "toolResponse",
          id: "call-1",
          toolResult: {
            status: "success",
            value: {
              content: [{ type: "text", text: "PRIVATE_TOOL_OUTPUT_SENTINEL" }],
            },
          },
        },
      ]);
      await exportTurn(
        privateConfig,
        "private-output-" + key,
        state,
        privateSource,
      );
      assert.ok(received.length);
      assert.ok(
        !JSON.stringify(received).includes("PRIVATE_TOOL_OUTPUT_SENTINEL"),
        key,
      );
    }
    received.length = 0;
    const hideToolInputs = await core.resolveConfig({
      agent: "goose",
      home: root,
      env: { ...env, FI_HIDE_INPUTS: "true" },
    });
    const privateArguments = structuredClone(source);
    const call = JSON.parse(privateArguments[1].content_json);
    call[0].toolCall.value.arguments = {
      command: "PRIVATE_TOOL_ARGUMENT_SENTINEL",
    };
    privateArguments[1].content_json = JSON.stringify(call);
    await exportTurn(hideToolInputs, "private-inputs", state, privateArguments);
    assert.ok(
      !JSON.stringify(received).includes("PRIVATE_TOOL_ARGUMENT_SENTINEL"),
    );
    assert.equal(attrs(tool.attributes)["tool.name"], "shell");
    assert.equal(attrs(tool.attributes)["tool_call.id"], "call-1");
    assert.equal(attrs(llms[0].attributes)["gen_ai.cost.total"], 0.01);
    assert.ok(attrs(llms[0].attributes)["output.value"].includes("shell"));
    received.length = 0;
    const hidden = await core.resolveConfig({
      agent: "goose",
      home: root,
      env: { ...env, FI_AGENT_CAPTURE_CONTENT: "false" },
    });
    await exportTurn(hidden, "session-2", state, source, "Stop");
    assert.ok(!JSON.stringify(received).includes("synthetic prompt"));
    assert.ok(!JSON.stringify(received).includes("synthetic answer"));
    assert.ok(!JSON.stringify(received).includes("synthetic tool error"));
    received.length = 0;
    const malformed = structuredClone(source);
    malformed[1].content_json = "{}";
    await assert.rejects(
      exportTurn(config, "bad-shape", state, malformed),
      /UNQUALIFIED_TRANSCRIPT_SCHEMA/,
    );
    assert.equal(received.length, 0);
    await exportTurn(
      config,
      "failed-provider",
      state,
      source.slice(0, 1),
      "Stop",
    );
    const incomplete = received.flatMap((p) =>
      p.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
    assert.equal(incomplete.length, 1);
    assert.equal(
      attrs(incomplete[0].attributes)["fi.agent.outcome"],
      "incomplete",
    );
    assert.ok(
      !("gen_ai.usage.input_tokens" in attrs(incomplete[0].attributes)),
    );
  } finally {
    await new Promise((r) => server.close(r));
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("logical high-water marks survive compaction and identical prompt text", () => {
  const db = new DatabaseSync(":memory:");
  db.exec(
    "CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_id TEXT, role TEXT, content_json TEXT, metadata_json TEXT, created_timestamp INTEGER)",
  );
  const add = (id, role, text) =>
    db
      .prepare(
        "INSERT INTO messages(session_id,message_id,role,content_json,metadata_json,created_timestamp) VALUES('s',?,?,?,?,1)",
      )
      .run(
        id,
        role,
        JSON.stringify([{ type: "text", text }]),
        JSON.stringify({ userVisible: true }),
      );
  add("prompt-1", "user", "same");
  add("model-1", "assistant", "answer");
  const completed = messageIndex(db, "s").map((r) => r.key);
  assert.equal(
    persistedPrompt(db, "s", messageIndex(db, "s"), completed, "same"),
    undefined,
  );
  db.exec("DELETE FROM messages");
  add("prompt-1", "user", "same");
  add("model-1", "assistant", "answer");
  add("prompt-2", "user", "same");
  const prompt = persistedPrompt(
    db,
    "s",
    messageIndex(db, "s"),
    completed,
    "same",
  );
  assert.equal(prompt.message_id, "prompt-2");
  add("model-2", "assistant", "new answer");
  assert.deepEqual(
    readRows(db, "s", completed).map((r) => r.message_id),
    ["prompt-2", "model-2"],
  );
  db.close();
});
