const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createServer } = require("node:http");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { setup, resolveConfig, disable } = require("@traceai/agent-core");
const {
  consume,
  usage,
  requestUsage,
  seal,
  unseal,
} = require("../dist/hook.js");

test("usage stays absent when unknown; encryption authenticates identity", () => {
  assert.equal(usage({}), undefined);
  assert.deepEqual(usage({ input_tokens: 7, output_tokens: 3 }), {
    input: 7,
    output: 3,
    cacheInInput: true,
    reasoningInOutput: true,
  });
  const key = Buffer.alloc(32, 1),
    state = { input: "synthetic private input" };
  const encrypted = seal(state, key, "turn-a");
  assert(!JSON.stringify(encrypted).includes("synthetic"));
  assert.deepEqual(unseal(encrypted, key, "turn-a"), state);
  assert.throws(() => unseal(encrypted, key, "turn-b"));
});

test("turn, model attempts, tools, errors and compaction; privacy, disable, dedup", async () => {
  const bodies = [];
  const server = createServer((req, res) => {
    let data = "";
    req.on("data", (b) => (data += b));
    req.on("end", () => {
      bodies.push(JSON.parse(data));
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "muse-unit-"));
  const env = {
    HOME: home,
    FI_API_KEY: "synthetic-api",
    FI_SECRET_KEY: "synthetic-secret",
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  };
  const options = { agent: "muse", home, env };
  try {
    const result = await setup({ ...options, configureOnly: true });
    assert.equal(result.configured, true);
    const config = await resolveConfig(options);
    const base = {
      session_id: "session-1",
      turn_id: "turn-1",
      model: "fake-model",
    };
    let time = Date.now();
    const emit = (hook_event_name, extra = {}) =>
      consume({ ...base, hook_event_name, ...extra }, config, (time += 10));
    await emit("UserPromptSubmit", {
      prompt: "Fixture prompt synthetic-secret",
    });
    await emit("PreLLMCall", {
      request_id: "req1",
      attempt: 1,
      step: 0,
      provider: "meta",
      messages: [{ text_preview: "fixture input" }],
    });
    await emit("PostLLMCall", {
      request_id: "req1",
      attempt: 1,
      step: 0,
      status: "success",
      output_text_preview: "fixture answer",
      usage: { input_tokens: 7, output_tokens: 3 },
    });
    await emit("PreToolUse", {
      tool_use_id: "tool1",
      tool_name: "bash",
      tool_input: { command: "false" },
    });
    await emit("PostToolUseFailure", {
      tool_use_id: "tool1",
      tool_name: "bash",
      error: "fixture tool failed",
    });
    await emit("PreCompact", { trigger: "auto" });
    await emit("PostCompact", { trigger: "auto" });
    const files = await fs.readdir(
      path.join(config.paths.stateDir, "muse-turns"),
    );
    const stored = await fs.readFile(
      path.join(
        config.paths.stateDir,
        "muse-turns",
        files.find((x) => x.endsWith(".json")),
      ),
      "utf8",
    );
    assert(!stored.includes("Fixture prompt"));
    assert(!stored.includes("synthetic-secret"));
    await emit("Stop", { last_assistant_message: "done" });
    await emit("Stop", { last_assistant_message: "duplicate" });
    const spans = bodies.flatMap((x) =>
      (x.resourceSpans || []).flatMap((r) =>
        (r.scopeSpans || []).flatMap((s) => s.spans || []),
      ),
    );
    assert.equal(spans.length, 4);
    const attrs = (s) =>
      Object.fromEntries(
        s.attributes.map((a) => [
          a.key,
          a.value.stringValue ?? a.value.intValue ?? a.value.boolValue,
        ]),
      );
    const root = spans.find((s) => !s.parentSpanId);
    assert(root);
    assert.equal(spans.filter((s) => !s.parentSpanId).length, 1);
    for (const span of spans.filter((s) => s !== root))
      assert.equal(span.parentSpanId, root.spanId);
    assert.equal(spans.find((s) => s.name === "bash").status.code, 2);
    assert.equal(attrs(spans.find((s) => s.name === "bash"))["tool_call.id"], "tool1");
    assert.equal(
      attrs(spans.find((s) => s.name === "muse.model-attempt.preview"))[
        "fi.agent.fidelity"
      ],
      "native-request",
    );
    assert(!JSON.stringify(bodies).includes("synthetic-secret"));
    const count = bodies.length;
    await disable(options);
    await emit("UserPromptSubmit", {
      turn_id: "disabled",
      prompt: "DO NOT STORE",
    });
    await emit("Stop", { turn_id: "disabled" });
    assert.equal(bodies.length, count);
    const structure = await resolveConfig({
      ...options,
      env: { ...env, FI_AGENT_CAPTURE_CONTENT: "false" },
    });
    assert.equal(structure.captureContent, false);
  } finally {
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  }
});

test("native hook counters are cumulative, with fail-closed ownership on gaps and retries", () => {
  const turn = {};
  const event = (step, input, output, attempt = 1) => ({
    hook_event_name: "PostLLMCall",
    session_id: "s",
    step,
    attempt,
    status: "success",
    usage: { input_tokens: input, output_tokens: output },
  });
  assert.equal(
    requestUsage(turn, { ...event(0, 11, 5), status: "tool_calls" }).input,
    11,
  );
  assert.equal(requestUsage(turn, event(1, 22, 10)).input, 11);
  assert.equal(requestUsage(turn, event(3, 44, 20)), undefined);
  assert.equal(turn.cumulative.input, 44);
  assert.equal(requestUsage(turn, event(4, 55, 25)), undefined);
  assert.equal(requestUsage({}, event(0, 11, 5, 2)), undefined);
});
