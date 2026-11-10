const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { DatabaseSync } = require("node:sqlite");
const core = require("@traceai/agent-core");
const { handleHook } = require("../dist/hook.js");
const attrs = (s) =>
  Object.fromEntries(
    (s.attributes || []).map((a) => [a.key, Object.values(a.value)[0]]),
  );

test("attempted Stop retains a turn; next prompt and SessionEnd finalize exactly once across rewrites", async (t) => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "goose-hooks-"));
  const original = { ...process.env };
  const received = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (b) => (body += b));
    req.on("end", () => {
      received.push(JSON.parse(body));
      res.setHeader("Content-Type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    process.env = original;
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  Object.assign(process.env, {
    GOOSE_PATH_ROOT: path.join(home, "goose"),
    FI_AGENT_CONFIG_DIR: path.join(home, "fi"),
    XDG_STATE_HOME: path.join(home, "state"),
    OTEL_TRACES_EXPORTER: "none",
  });
  const options = { agent: "goose", projectDir: home };
  await core.setup({
    ...options,
    projectName: "test",
    credentials: { apiKey: "synthetic", secretKey: "synthetic-secret" },
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/traces`,
    configureOnly: true,
  });
  const config = await core.resolveConfig(options);
  await core.updateJson(
    path.join(config.paths.configDir, "goose-plugin.json"),
    () => ({ schemaVersion: 1, userScope: true }),
  );
  const sessions = path.join(process.env.GOOSE_PATH_ROOT, "data/sessions");
  await fs.mkdir(sessions, { recursive: true, mode: 0o700 });
  const file = path.join(sessions, "sessions.db");
  const db = new DatabaseSync(file);
  t.after(() => db.close());
  await fs.chmod(file, 0o600);
  db.exec(
    "CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_id TEXT, role TEXT, content_json TEXT, metadata_json TEXT, created_timestamp INTEGER)",
  );
  const add = (id, role, text) =>
    db
      .prepare(
        "INSERT INTO messages(session_id,message_id,role,content_json,metadata_json,created_timestamp) VALUES('s',?,?,?,?,?)",
      )
      .run(
        id,
        role,
        JSON.stringify([{ type: "text", text }]),
        JSON.stringify({
          userVisible: true,
          ...(role === "assistant"
            ? {
                inference: { provider: "openai", requestedModel: "test-model" },
                usage: { inputTokens: 100, outputTokens: 10 },
              }
            : {}),
        }),
        Math.floor(Date.now() / 1000),
      );
  const hook = (event, message) =>
    handleHook({
      event,
      session_id: "s",
      working_dir: event === "SessionEnd" ? undefined : home,
      message,
    });
  const spans = () =>
    received.flatMap((b) =>
      b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((sc) => sc.spans)),
    );
  received.length = 0;
  assert.equal((await core.resolveConfig(options)).enabled, true);
  const stateDir = path.join(
    path.dirname(config.paths.user),
    "futureagi-sessions",
  );
  await fs.mkdir(stateDir, { mode: 0o700 });
  const stateFile = path.join(stateDir, core.contentHash("s") + ".json");
  const { lock } = require("proper-lockfile");
  const release = await lock(stateFile, {
    realpath: false,
    lockfilePath: stateFile + ".hook-lock",
  });
  const timer = setTimeout(() => void release(), 120);
  await hook("UserPromptSubmit", "repeat"); // Must wait for the overlapping owner.
  clearTimeout(timer);
  assert.equal((await core.readJson(stateFile)).pending, true);
  add("p1", "user", "repeat");
  add("m1", "assistant", "first answer");
  await hook("Stop");
  const firstStop = (await core.readJson(stateFile)).stoppedAt;
  assert.equal(spans().length, 0);
  await new Promise((r) => setTimeout(r, 150));
  add("p2", "user", "repeat");
  await hook("UserPromptSubmit", "repeat"); // State-machine post-persistence path.
  assert.equal(
    spans().length,
    2,
    JSON.stringify(
      await core.readJson(core.deliveryStatePath(config)),
    ),
  );
  assert.equal(
    attrs(spans().find((s) => !s.parentSpanId))["input.value"],
    "repeat",
  );
  assert.ok(
    Math.abs(
      Number(spans().find((s) => !s.parentSpanId).endTimeUnixNano) / 1e6 -
        firstStop,
    ) < 2,
  );
  add("m2", "assistant", "second answer");
  await hook("Stop");
  assert.equal(spans().length, 2);
  // A rejected Stop continues in the same turn, even through conversation replacement.
  const before = db
    .prepare(
      "SELECT session_id,message_id,role,content_json,metadata_json,created_timestamp FROM messages ORDER BY id",
    )
    .all();
  db.exec("DELETE FROM messages");
  for (const r of before)
    db.prepare(
      "INSERT INTO messages(session_id,message_id,role,content_json,metadata_json,created_timestamp) VALUES(?,?,?,?,?,?)",
    ).run(...Object.values(r));
  const rejectedStop = (await core.readJson(stateFile)).stoppedAt;
  await new Promise((r) => setTimeout(r, 50));
  await handleHook({
    event: "PreToolUse",
    session_id: "s",
    working_dir: home,
    tool_call_id: "after-rejected-stop",
  });
  assert.equal((await core.readJson(stateFile)).stoppedAt, undefined);
  add("m3", "assistant", "continuation answer");
  await hook("Stop");
  const finalStop = (await core.readJson(stateFile)).stoppedAt;
  assert.ok(finalStop > rejectedStop);
  await new Promise((r) => setTimeout(r, 150));
  // Duplicate Stop does not add idle time. SessionEnd has no working_dir.
  await hook("Stop");
  assert.equal((await core.readJson(stateFile)).stoppedAt, finalStop);
  await Promise.all([hook("SessionEnd"), hook("SessionEnd")]);
  assert.ok(
    Math.abs(
      Number(spans().filter((s) => !s.parentSpanId)[1].endTimeUnixNano) / 1e6 -
        finalStop,
    ) < 2,
  );
  const roots = spans().filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 2);
  assert.equal(new Set(roots.map((s) => s.traceId)).size, 2);
  assert.equal(
    spans().filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    3,
  );
  assert.equal(
    spans().reduce(
      (n, s) => n + Number(attrs(s)["gen_ai.usage.input_tokens"] || 0),
      0,
    ),
    300,
  );
  await hook("SessionEnd");
  await hook("Stop");
  assert.equal(spans().length, 5);
  await fs.mkdir(stateFile + ".hook-lock");
  await fs.utimes(stateFile + ".hook-lock", new Date(0), new Date(0));
  await hook("SessionEnd");
  await assert.rejects(fs.stat(stateFile + ".hook-lock"), { code: "ENOENT" });
  await handleHook({ event: "UnknownFutureEvent", session_id: "s" });
  assert.equal(
    (await core.readJson(core.deliveryStatePath(config)))
      .lastError,
    "UNQUALIFIED_HOOK_SCHEMA",
  );
  assert.equal(
    (await core.inspectStatus(options)).delivery.lastError,
    "UNQUALIFIED_HOOK_SCHEMA",
  );
});
