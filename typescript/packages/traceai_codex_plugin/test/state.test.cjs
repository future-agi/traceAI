const { test } = require("node:test"),
  assert = require("node:assert/strict"),
  fs = require("node:fs/promises"),
  os = require("node:os"),
  path = require("node:path");
const { EventStore } = require("../dist/state.js");
const { RolloutReader } = require("../dist/rollout.js");
async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "codex-state-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  return {
    identityKey: "a".repeat(64),
    generation: 1,
    endpoint: "http://127.0.0.1:1/v1/traces",
    projectName: "test",
    captureContent: true,
    hideInputs: false,
    hideOutputs: false,
    paths: {
      configDir: dir,
      stateDir: path.join(dir, "state"),
      user: path.join(dir, "futureagi.json"),
    },
  };
}
const e = {
  hook_event_name: "UserPromptSubmit",
  session_id: "s",
  turn_id: "t",
  cwd: "/work",
  prompt: "private transcript content",
  receivedAt: 1,
};
test("private encrypted bounded state, destination binding, terminal claim dedup", async (t) => {
  const config = await fixture(t),
    store = new EventStore(config);
  await store.append(e);
  const file = path.join(store.dir, (await fs.readdir(store.dir))[0]);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  assert(!(await fs.readFile(file, "utf8")).includes(e.prompt));
  const changed = new EventStore({
    ...config,
    endpoint: "http://127.0.0.1:2/v1/traces",
  });
  assert.equal(await changed.take(e), undefined);
  const [a, b] = await Promise.all([store.take(e), store.take(e)]);
  assert.equal([a, b].filter(Boolean).length, 1);
  await store.append(e);
  assert.equal(await store.take(e), undefined);
});
test("state limits discard content rather than write an unbounded journal", async (t) => {
  const config = await fixture(t),
    store = new EventStore(config);
  for (let i = 0; i < 30; i++)
    await store.append({ ...e, prompt: "x".repeat(20000) });
  const state = await store.take(e);
  assert.equal(
    state.gap,
    false,
    "bounded content eviction is not a lifecycle gap",
  );
  assert.equal(state.events.length, 30);
  assert(state.events.some((event) => event.content_truncated));
  assert(Buffer.byteLength(JSON.stringify(state)) <= 42000);
});
test("verbose turns retain every request, tool, terminal record and usage counter", async (t) => {
  const config = await fixture(t),
    store = new EventStore(config);
  await store.append(e);
  for (let i = 0; i < 13; i++) {
    await store.append({
      ...e,
      hook_event_name: "PreModelRequest",
      prompt: undefined,
    });
    await store.append({
      ...e,
      hook_event_name: "PostModelRequest",
      prompt: undefined,
      status: "completed",
      token_usage: { input_tokens: 20, output_tokens: 8 },
    });
    if (i < 12) {
      await store.append({
        ...e,
        hook_event_name: "PreToolUse",
        prompt: undefined,
        tool_name: "exec_command",
        tool_use_id: "call_" + i,
        tool_input: "printf %010000d " + i,
      });
      await store.append({
        ...e,
        hook_event_name: "PostToolUse",
        prompt: undefined,
        tool_use_id: "call_" + i,
        tool_response: "x".repeat(10000),
      });
    }
  }
  await store.append({ ...e, hook_event_name: "Stop", prompt: undefined });
  const state = await store.take(e);
  assert.equal(state.gap, false);
  assert.equal(state.events.length, 52);
  assert.equal(state.events.at(-1).hook_event_name, "Stop");
  assert.equal(
    state.events.filter((event) => event.hook_event_name === "PostToolUse")
      .length,
    12,
  );
  assert.equal(
    state.events.reduce(
      (sum, event) => sum + (event.token_usage?.input_tokens ?? 0),
      0,
    ),
    260,
  );
  assert.equal(
    state.events.reduce(
      (sum, event) => sum + (event.token_usage?.output_tokens ?? 0),
      0,
    ),
    104,
  );
  assert(state.events.some((event) => event.content_truncated));
  assert(Buffer.byteLength(JSON.stringify(state)) <= 42000);
});

test("structural overflow retains terminal records and reports loss instead of stranding a turn", async (t) => {
  const store = new EventStore(await fixture(t));
  for (let i = 0; i < 130; i++)
    await store.append({
      ...e,
      hook_event_name: "PreModelRequest",
      prompt: undefined,
    });
  await store.append({ ...e, hook_event_name: "Stop", prompt: undefined });
  const state = await store.take(e);
  assert.equal(state.events.length, 128);
  assert.equal(state.events.at(-1).hook_event_name, "Stop");
  assert.equal(state.gap, true);
});

test("rollout cursor handles partial UTF-8, malformed lines, and session mismatch", async (t) => {
  const config = await fixture(t);
  const sessions = path.join(config.paths.configDir, "sessions");
  await fs.mkdir(sessions, { mode: 0o700 });
  const file = path.join(sessions, "rollout-s.jsonl");
  const header =
    JSON.stringify({ type: "session_meta", payload: { id: "s" } }) + "\n";
  const line =
    JSON.stringify({
      timestamp: new Date().toISOString(),
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        content: [{ text: "hello 世界" }],
      },
    }) + "\n";
  const bytes = Buffer.from(line),
    cut = bytes.indexOf(Buffer.from("世")) + 1;
  await fs.writeFile(
    file,
    Buffer.concat([Buffer.from(header), bytes.subarray(0, cut)]),
    { mode: 0o600 },
  );
  const reader = new RolloutReader(file, "s", config);
  await reader.read();
  assert.equal(reader.messages.length, 0);
  await fs.appendFile(file, bytes.subarray(cut));
  await reader.read();
  assert.equal(reader.messages[0].text, "hello 世界");
  await reader.checkpoint();
  const resumed = new RolloutReader(file, "s", config);
  await resumed.read();
  assert.equal(resumed.messages.length, 0);
  const wrong = new RolloutReader(file, "other", config);
  await wrong.read();
  assert(wrong.gap);
  assert.equal(wrong.messages.length, 0);
  await fs.appendFile(file, "not-json\n");
  await resumed.read();
  assert(resumed.gap);
});
