const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, fs, path, payload } = require("./helpers.cjs");
const { appendEvent, routeId } = require("../dist/state.js");
const { normalizeEvent } = require("../dist/events.js");
test("state is destination-bound, encrypted, bounded, terminal-claimed once and rejects tampering", async (t) => {
  const f = await fixture(t);
  const event = (name, extra = {}) =>
    normalizeEvent(payload(name, extra), name, f.config);
  const prompt = event("beforeSubmitPrompt", { prompt: "SECRET_CAPTURE" });
  assert.equal(await appendEvent(f.config, prompt), undefined);
  assert.equal(await appendEvent(f.config, prompt), undefined);
  const snapshot = await appendEvent(
    f.config,
    event("stop", { status: "completed" }),
  );
  assert.equal(snapshot.events.length, 2);
  assert.equal(snapshot.events[0].input, "SECRET_CAPTURE");
  assert.equal(
    await appendEvent(f.config, event("stop", { status: "completed" })),
    undefined,
  );
  const altered = {
    ...f.config,
    credentials: f.config.credentials,
    projectName: "other",
  };
  assert.notEqual(routeId(altered), routeId(f.config));
  assert.notEqual(
    routeId({
      ...f.config,
      credentials: f.config.credentials,
      paths: { ...f.config.paths, projectBinding: "new-project" },
    }),
    routeId(f.config),
  );
  const second = event("beforeSubmitPrompt", {
    generation_id: "other",
    prompt: "tamper secret",
  });
  await appendEvent(f.config, second);
  const dir = path.join(f.config.paths.stateDir, "cursor-turns");
  for (const name of await fs.readdir(dir)) {
    const file = path.join(dir, name),
      raw = JSON.parse(await fs.readFile(file));
    if (raw.encrypted) {
      raw.encrypted = Buffer.alloc(48).toString("base64");
      await fs.writeFile(file, JSON.stringify(raw), { mode: 0o600 });
    }
  }
  await assert.rejects(
    appendEvent(f.config, {
      ...event("stop", { status: "completed" }),
      generation: "other",
    }),
  );
});
test("separate response slot preserves previously observed usage and transcript metadata", async (t) => {
  const f = await fixture(t);
  const event = (name, extra = {}) =>
    normalizeEvent(payload(name, extra), name, f.config);
  await appendEvent(f.config, event("beforeSubmitPrompt", { prompt: "safe" }));
  await appendEvent(
    f.config,
    event("afterAgentResponse", {
      text: "first response",
      input_tokens: 100,
      output_tokens: 10,
      model: "fixture-model",
      transcript_path: "/synthetic/transcript.jsonl",
    }),
  );
  await appendEvent(
    f.config,
    event("afterAgentResponse", { text: "latest response" }),
  );
  const snapshot = await appendEvent(
    f.config,
    event("stop", { status: "completed" }),
  );
  const response = snapshot.events.find(
    (e) => e.event === "afterAgentResponse",
  );
  assert.equal(snapshot.events.length, 3);
  assert.equal(response.output, "latest response");
  assert.equal(response.usage.input, 100);
  assert.equal(response.usage.output, 10);
  assert.equal(response.model, "fixture-model");
  assert.equal(response.transcript, "/synthetic/transcript.jsonl");
  assert.equal(snapshot.events.at(-1).event, "stop");
});
test("state cannot follow symlinks; content limits retain structural events without raw overflow", async (t) => {
  const f = await fixture(t),
    turn = "bounded";
  for (let i = 0; i < 10; i++)
    await appendEvent(
      f.config,
      normalizeEvent(
        payload("postToolUse", {
          generation_id: turn,
          tool_use_id: `tool-${i}`,
          tool_input: "i".repeat(20000),
          tool_output: "o".repeat(20000),
        }),
        "postToolUse",
        f.config,
      ),
    );
  const state = await appendEvent(
    f.config,
    normalizeEvent(
      payload("stop", { generation_id: turn, status: "completed" }),
      "stop",
      f.config,
    ),
  );
  assert.equal(state.gap, "TURN_CONTENT_LIMIT");
  assert.equal(state.events.length, 11);
  const configKey = path.join(f.config.paths.configDir, "cursor-state-key");
  assert.equal((await fs.stat(configKey)).mode & 0o777, 0o600);
  await fs.rename(configKey, configKey + ".real");
  await fs.symlink(configKey + ".real", configKey);
  await assert.rejects(
    appendEvent(
      f.config,
      normalizeEvent(
        payload("stop", { status: "completed" }),
        "stop",
        f.config,
      ),
    ),
  );
});
