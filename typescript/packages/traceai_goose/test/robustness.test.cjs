const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { DatabaseSync } = require("node:sqlite");
const core = require("@traceai/agent-core");
const { exportTurn } = require("../dist/transcript.js");
const { handleHook } = require("../dist/hook.js");
const attrs = (s) =>
  Object.fromEntries(
    (s.attributes || []).map((a) => [a.key, Object.values(a.value)[0]]),
  );

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "goose-recovery-"));
  const original = { ...process.env };
  const received = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (b) => (body += b));
    req.on("end", () => {
      received.push(body);
      res.setHeader("Content-Type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    process.env = original;
    await new Promise((r) => server.close(r));
    await fs.rm(root, { recursive: true, force: true });
  });
  Object.assign(process.env, {
    GOOSE_PATH_ROOT: path.join(root, "goose"),
    FI_AGENT_CONFIG_DIR: path.join(root, "fi"),
    XDG_STATE_HOME: path.join(root, "state"),
    OTEL_TRACES_EXPORTER: "none",
  });
  const options = { agent: "goose", projectDir: root };
  const setup = await core.setup({
    ...options,
    projectName: "synthetic-regressions",
    credentials: { apiKey: "fixture-api", secretKey: "fixture-secret" },
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/traces`,
    configureOnly: true,
  });
  assert.equal(setup.configured, true);
  const config = await core.resolveConfig(options);
  await core.updateJson(
    path.join(config.paths.configDir, "goose-plugin.json"),
    () => ({ schemaVersion: 1, userScope: true }),
  );
  const sessions = path.join(process.env.GOOSE_PATH_ROOT, "data/sessions");
  await fs.mkdir(sessions, { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(path.join(sessions, "sessions.db"));
  t.after(() => db.close());
  db.exec(
    "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT, role TEXT, content_json TEXT, metadata_json TEXT, created_timestamp INTEGER)",
  );
  const stateFile = path.join(
    path.dirname(config.paths.user),
    "futureagi-sessions",
    core.contentHash("s") + ".json",
  );
  received.length = 0;
  return {
    root,
    config,
    received,
    stateFile,
    hook: (event, rest = {}) =>
      handleHook({
        event,
        session_id: "s",
        working_dir: root,
        message: event === "UserPromptSubmit" ? "request" : undefined,
        ...rest,
      }),
    add: (rows) => {
      for (const r of rows)
        db.prepare("INSERT INTO messages VALUES(?,?,?,?,?,?,?)").run(
          r.id,
          "s",
          r.message_id,
          r.role,
          r.content_json,
          r.metadata_json,
          r.created_timestamp,
        );
    },
    spans: () =>
      received.flatMap((body) =>
        JSON.parse(body).resourceSpans.flatMap((r) =>
          r.scopeSpans.flatMap((s) => s.spans),
        ),
      ),
  };
}
const row = (id, role, parts, meta = {}) => ({
  id,
  message_id: "message-" + id,
  role,
  content_json: JSON.stringify(parts),
  metadata_json: JSON.stringify(meta),
  created_timestamp: Math.floor(Date.now() / 1000),
});
const inference = {
  inference: { provider: "openai", requestedModel: "fixture-model" },
  usage: { inputTokens: 8, outputTokens: 2 },
};
const simpleRows = () => [
  row(1, "user", [{ type: "text", text: "request" }], { userVisible: true }),
  row(2, "assistant", [{ type: "text", text: "completed" }], inference),
];

test("complete OTLP payload redacts structured aliases and JSON results before cross-span propagation", async (t) => {
  const f = await fixture(t);
  const aliases = [
    "password",
    "api_key",
    "client_secret",
    "access_token",
    "PASSWORD",
    "apiKey",
    "API-KEY",
    "API KEY",
    "ClientSecret",
    "CLIENT-SECRET",
    "client secret",
    "AccessToken",
    "ACCESS-TOKEN",
    "access token",
    "FI_SECRET_KEY",
    "secret_key",
  ];
  const secrets = aliases.map((_, i) => `SYNTHETIC_ARGUMENT_${i}_END`);
  const args = {
    safe: "ordinary-argument",
    nested: [Object.fromEntries(aliases.map((key, i) => [key, secrets[i]]))],
    password: { nested: "SYNTHETIC_OBJECT_SECRET" },
  };
  const resultText =
    'result: {"password":"SYNTHETIC_RESULT_PASSWORD with spaces","nested":[{"access-token":{"data":"SYNTHETIC_RESULT_OBJECT"}}],"safe":"ordinary-result"}';
  for (const failed of [false, true]) {
    f.received.length = 0;
    const rows = [
      row(
        1,
        "user",
        [{ type: "text", text: 'request {"apiKey":"SYNTHETIC_PROMPT_KEY"}' }],
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
              value: { name: "example", arguments: args },
            },
          },
        ],
        inference,
      ),
      row(3, "user", [
        {
          type: "toolResponse",
          id: "call-1",
          toolResult: failed
            ? { status: "error", error: resultText }
            : {
                status: "success",
                value: { content: [{ type: "text", text: resultText }] },
              },
        },
      ]),
      row(
        4,
        "assistant",
        [
          {
            type: "text",
            text: 'answer {"client_secret":"SYNTHETIC_ANSWER_SECRET"}',
          },
        ],
        inference,
      ),
    ];
    await exportTurn(
      f.config,
      "privacy-" + failed,
      {
        schemaVersion: 2,
        baseline: [],
        completed: [],
        turnId: "turn-" + failed,
        pending: true,
        stopped: true,
        start: Date.now() - 500,
        generation: 0,
        policy: "test",
        tools: {},
      },
      rows,
    );
    const spans = f.spans();
    assert.equal(spans.length, 4);
    const serialized = f.received.join("\n");
    for (const secret of [
      ...secrets,
      "SYNTHETIC_OBJECT_SECRET",
      "SYNTHETIC_RESULT_PASSWORD",
      "SYNTHETIC_RESULT_OBJECT",
      "SYNTHETIC_PROMPT_KEY",
      "SYNTHETIC_ANSWER_SECRET",
      "with spaces",
    ]) {
      assert.equal(
        serialized.includes(secret),
        false,
        "credential escaped into complete OTLP: " + secret,
      );
    }
    const llms = spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
    const tool = spans.find((s) => attrs(s)["fi.span.kind"] === "TOOL");
    for (const text of [
      attrs(llms[0])["output.value"],
      attrs(tool)["input.value"],
      attrs(tool)["output.value"],
      attrs(llms[1])["input.value"],
    ]) {
      assert.ok(text.includes("[REDACTED]"));
    }
    // Structured arguments preserve safe fields. In opaque text containing an
    // object-valued credential, the unified core deliberately removes the tail
    // rather than guessing where that secret ends.
    assert.ok(attrs(llms[0])["output.value"].includes("ordinary-argument"));
    assert.ok(attrs(tool)["input.value"].includes("ordinary-argument"));
    assert.equal(attrs(llms[1])["input.value"], attrs(tool)["output.value"]);
    assert.equal(tool.status.code === 2, failed);
    assert.equal(args.nested[0].password, secrets[0]); // Host objects are never changed.
  }
});

test("saved Anthropic input already includes cache reads and writes", async (t) => {
  const f = await fixture(t);
  const rows = [
    row(1, "user", [{ type: "text", text: "request" }], { userVisible: true }),
    row(2, "assistant", [{ type: "text", text: "answer" }], {
      inference: { provider: "anthropic", requestedModel: "fixture-model" },
      usage: { inputTokens: 350, outputTokens: 10, cacheReadTokens: 200, cacheWriteTokens: 50 },
    }),
  ];
  await exportTurn(f.config, "cache", {
    schemaVersion: 2, baseline: [], completed: [], turnId: "cache-turn",
    pending: true, stopped: true, start: Date.now() - 500,
    generation: f.config.generation, policy: "test", tools: {},
  }, rows);
  const spans = f.spans();
  assert.equal(spans.length, 2);
  const model = attrs(spans.find((s) => attrs(s)["fi.span.kind"] === "LLM"));
  assert.equal(model["gen_ai.usage.input_tokens"], 350);
  assert.equal(model["gen_ai.usage.output_tokens"], 10);
  assert.equal(model["gen_ai.usage.total_tokens"], 360);
  assert.equal(model["gen_ai.usage.cache_read_tokens"], 200);
  assert.equal(model["gen_ai.usage.cache_write_tokens"], 50);
  assert.equal(spans.reduce((n, s) => n + (attrs(s)["gen_ai.usage.input_tokens"] || 0), 0), 350);
});

test("explicit cancellation owns the root outcome without inventing cancellation on exit", async (t) => {
  for (const reason of ["user_cancel", "exit", "unknown_reason", undefined]) {
    await t.test(String(reason), async (t) => {
      const f = await fixture(t);
      await f.hook("UserPromptSubmit");
      f.add(simpleRows());
      // Even a complete response without Stop is not proof of successful completion.
      await f.hook("SessionEnd", { reason });
      const roots = f.spans().filter((s) => !s.parentSpanId);
      assert.equal(roots.length, 1);
      assert.equal(attrs(roots[0])["fi.agent.outcome"], reason === "user_cancel" ? "interrupted" : "incomplete");
      assert.equal(f.spans().filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 1);
      await f.hook("SessionEnd", { reason });
      assert.equal(f.spans().length, 2);
    });
  }
});

test("cancelled roots retain interrupted when tools fail or have no response", async (t) => {
  for (const failed of [false, true]) {
    await t.test(String(failed), async (t) => {
      const f = await fixture(t);
      await f.hook("UserPromptSubmit");
      f.add([
        simpleRows()[0],
        row(2, "assistant", [{ type: "toolRequest", id: "call", toolCall: {
          status: "success", value: { name: "read", arguments: {} },
        } }], inference),
        ...(failed ? [row(3, "user", [{ type: "toolResponse", id: "call", toolResult: {
          status: "error", error: "synthetic failure",
        } }])] : []),
      ]);
      await f.hook("SessionEnd", { reason: "user_cancel" });
      const root = f.spans().find((s) => !s.parentSpanId);
      const tool = f.spans().find((s) => attrs(s)["fi.span.kind"] === "TOOL");
      assert.equal(attrs(root)["fi.agent.outcome"], "interrupted");
      assert.equal(attrs(tool)["fi.agent.outcome"], failed ? "error" : "incomplete");
    });
  }
});

async function bundledEnd(reason = "exit") {
  const child = spawn(process.execPath, [path.join(__dirname, "../dist/hook.cjs")], {
    env: { ...process.env, NODE_NO_WARNINGS: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (b) => (output += b));
  child.stderr.on("data", (b) => (output += b));
  const exited = once(child, "exit");
  child.stdin.end(JSON.stringify({ event: "SessionEnd", session_id: "s", reason }));
  assert.deepEqual(await exited, [0, null]);
  assert.equal(output, "");
}

test("bundled terminal hook waits six seconds for responses and releases its SQLite read transaction", async (t) => {
  const f = await fixture(t);
  await f.hook("UserPromptSubmit");
  f.add([simpleRows()[0]]);
  await f.hook("PreToolUse", { tool_call_id: "call" });
  await f.hook("PostToolUse", { tool_call_id: "call" });
  await f.hook("Stop");
  const stoppedAt = (await core.readJson(f.stateFile)).stoppedAt;
  const write = (async () => {
    await new Promise((r) => setTimeout(r, 6000));
    assert.equal(f.spans().length, 0, "must not export an early incomplete root");
    f.add([
      row(2, "assistant", [{
        type: "toolRequest",
        id: "call",
        toolCall: { status: "success", value: { name: "read", arguments: {} } },
      }], inference),
      row(3, "assistant", [{ type: "text", text: "completed" }], inference),
    ]);
    // The final generation alone is insufficient while a tool response is uncommitted.
    await new Promise((r) => setTimeout(r, 150));
    assert.equal(f.spans().length, 0);
    f.add([row(4, "user", [{
      type: "toolResponse",
      id: "call",
      toolResult: {
        status: "success",
        value: { content: [{ type: "text", text: "read result" }] },
      },
    }])]);
  })();
  await Promise.all([bundledEnd(), write]);
  const spans = f.spans();
  const root = spans.find((s) => !s.parentSpanId);
  assert.equal(spans.length, 4);
  assert.equal(attrs(root)["fi.agent.outcome"], "success");
  assert.ok(Math.abs(Number(root.endTimeUnixNano) / 1e6 - stoppedAt) < 2,
    "SQLite persistence wait must not inflate turn duration");
  assert.equal(spans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 2);
  assert.equal(spans.reduce((n, s) => n + (attrs(s)["gen_ai.usage.input_tokens"] || 0), 0), 16);
  assert.equal(spans.reduce((n, s) => n + (attrs(s)["gen_ai.usage.output_tokens"] || 0), 0), 4);
  assert.equal((await core.readJson(f.stateFile)).pending, false);
  await bundledEnd();
  assert.equal(f.spans().length, 4);
});

test("missing terminal response stays incomplete and returns within the hook budget", async (t) => {
  const f = await fixture(t);
  await f.hook("UserPromptSubmit");
  f.add([simpleRows()[0]]);
  await f.hook("Stop");
  const start = Date.now();
  await bundledEnd();
  assert.ok(Date.now() - start < 9000);
  const roots = f.spans().filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1);
  assert.equal(attrs(roots[0])["fi.agent.outcome"], "incomplete");
  assert.equal((await core.readJson(f.stateFile)).pending, false);
});

test("a disable during terminal persistence wait prevents export", async (t) => {
  const f = await fixture(t);
  await f.hook("UserPromptSubmit");
  f.add([simpleRows()[0]]);
  await f.hook("Stop");
  const change = (async () => {
    await new Promise((r) => setTimeout(r, 100));
    await core.disable({ agent: "goose", projectDir: f.root });
    f.add([simpleRows()[1]]);
  })();
  await Promise.all([f.hook("SessionEnd"), change]);
  assert.equal(f.spans().length, 0);
  assert.equal((await core.readJson(f.stateFile)).pending, false);
});

// Kill a real writer exactly after acquiring its claim, not a hand-made lock fixture.
async function killWriter(t, f, mode) {
  const child = spawn(
    process.execPath,
    [
      "-e",
      `
    const fs = require('node:fs/promises');
    const [mode, file, root] = process.argv.slice(1);
    const open = fs.open;
    fs.open = async function(name, ...args) {
      const handle = await open.call(this, name, ...args);
      if ((mode === 'legacy' && name === file + '.lock') ||
          (mode === 'current' && name.startsWith(file + '.') && name.endsWith('.tmp'))) {
        process.send({ acquired: mode });
        setInterval(() => {}, 1000);
        await new Promise(() => {});
      }
      return handle;
    };
    const pending = mode === 'legacy'
      // Reproduce the old writer's O_EXCL claim, not the unified recoverable lease.
      ? fs.open(file + '.lock', 'wx', 0o600)
      : require('../dist/hook.js').handleHook({ event: 'PreToolUse', session_id: 's', working_dir: root, tool_call_id: 'crashed-call' });
    pending.then(() => process.exit(9), () => process.exit(10));
  `,
      mode,
      f.stateFile,
      f.root,
    ],
    { cwd: __dirname, stdio: ["ignore", "ignore", "pipe", "ipc"] },
  );
  t.after(() => {
    if (child.exitCode === null && child.signalCode === null)
      child.kill("SIGKILL");
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("writer did not acquire lock"));
    }, 10000);
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", () => {
      clearTimeout(timer);
      reject(new Error("writer exited before capture"));
    });
    child.once("message", (message) => {
      clearTimeout(timer);
      assert.equal(message.acquired, mode);
      resolve();
    });
  });
  const exited = once(child, "exit");
  child.kill("SIGKILL");
  assert.deepEqual(await exited, [null, "SIGKILL"]);
}

test("killed legacy JSON writer cannot wedge Stop, SessionEnd or the next prompt", async (t) => {
  const f = await fixture(t);
  await f.hook("UserPromptSubmit");
  const before = await core.readJson(f.stateFile);
  f.add(simpleRows());
  await killWriter(t, f, "legacy");
  await fs.utimes(f.stateFile + ".lock", new Date(0), new Date(0));
  await f.hook("Stop");
  assert.equal((await core.readJson(f.stateFile)).stopped, true);
  await f.hook("SessionEnd");
  assert.equal(f.spans().length, 2);
  assert.equal((await core.readJson(f.stateFile)).pending, false);
  await f.hook("UserPromptSubmit");
  const after = await core.readJson(f.stateFile);
  assert.equal(after.pending, true);
  assert.notEqual(after.turnId, before.turnId);
  assert.equal((await fs.stat(f.stateFile)).mode & 0o777, 0o600);
  assert.ok(await fs.stat(f.stateFile + ".lock")); // Recovery never needs to remove it.
});

test("killed session writer recovers automatically through the sole heartbeat lock", async (t) => {
  const f = await fixture(t);
  await f.hook("UserPromptSubmit");
  f.add(simpleRows());
  const previous = await fs.readFile(f.stateFile, "utf8");
  await killWriter(t, f, "current");
  assert.equal(await fs.readFile(f.stateFile, "utf8"), previous); // Atomic write did not commit.
  await assert.rejects(fs.stat(f.stateFile + ".lock"), { code: "ENOENT" });
  // Exercise the real 10-second stale-owner expiry, with no manual lock removal.
  await new Promise((r) => setTimeout(r, 11000));
  await f.hook("Stop");
  await Promise.all([f.hook("SessionEnd"), f.hook("SessionEnd")]);
  assert.equal(f.spans().length, 2);
  assert.equal(f.spans().filter((s) => !s.parentSpanId).length, 1);
  assert.equal((await core.readJson(f.stateFile)).pending, false);
  assert.equal((await fs.stat(f.stateFile)).mode & 0o777, 0o600);
  await assert.rejects(fs.stat(f.stateFile + ".hook-lock"), { code: "ENOENT" });
});
