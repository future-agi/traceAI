const { test } = require("node:test"),
  assert = require("node:assert/strict"),
  fs = require("node:fs/promises"),
  os = require("node:os"),
  path = require("node:path");
const { correlate } = require("../dist/ancestry.js");
test("only explicit native child identity remaps its hooks; child stop cannot finalize parent", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "codex-parent-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const config = {
    generation: 0,
    endpoint: "http://localhost:1",
    projectName: "test",
    paths: { configDir: dir },
  };
  const base = { cwd: "/work", receivedAt: 1 };
  await correlate(
    {
      ...base,
      hook_event_name: "UserPromptSubmit",
      session_id: "parent",
      turn_id: "parent-turn",
    },
    config,
  );
  await correlate(
    {
      ...base,
      hook_event_name: "SubagentStart",
      session_id: "parent",
      turn_id: "child-turn",
      agent_id: "child",
    },
    config,
  );
  const child = await correlate(
    {
      ...base,
      hook_event_name: "PreModelRequest",
      session_id: "child",
      turn_id: "child-turn",
    },
    config,
  );
  assert.equal(child.session_id, "parent");
  assert.equal(child.turn_id, "parent-turn");
  assert.equal(child.agent_id, "child");
  assert.equal(child.source_session_id, "child");
  assert.equal(child.source_turn_id, "child-turn");
  assert.equal(
    (
      await correlate(
        {
          ...base,
          hook_event_name: "Stop",
          session_id: "child",
          turn_id: "child-turn",
        },
        config,
      )
    ).hook_event_name,
    "SubagentStop",
  );
  assert.equal(
    await correlate(
      { ...base, hook_event_name: "SessionEnd", session_id: "child" },
      config,
    ),
    undefined,
  );
  const other = {
    ...base,
    hook_event_name: "PreModelRequest",
    session_id: "unlinked",
    turn_id: "other",
  };
  assert.deepEqual(await correlate(other, config), other);
  const changed = { ...config, generation: 1 };
  assert.equal(
    (
      await correlate(
        {
          ...base,
          hook_event_name: "PreModelRequest",
          session_id: "child",
          turn_id: "child-turn",
        },
        changed,
      )
    ).session_id,
    "child",
  );
});
