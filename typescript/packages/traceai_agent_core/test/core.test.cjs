const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createHmac } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const c = require("../dist/src/index.js");
const { observe } = require("../dist/src/client.js");
const vectors = require("../../../../integrations/coding-agents/fixtures/identity/v1.json");
const key = Buffer.from(vectors.test_key_hex_SYNTHETIC_ONLY, "hex");
function fact() {
  const work = c.workId(key, "fixture", "request", ["s"], ["t"], ["r"]);
  return {
    schemaVersion: 1,
    sourceWorkId: work,
    identityProfile: "fixture-v1",
    sessionId: c.sessionId(key, "fixture", ["s"]),
    turnId: c.workId(key, "fixture", "turn", ["s"], ["t"]),
    traceId: c.traceId(work),
    spanId: c.spanId(work),
    agent: "reference",
    surface: "fixture",
    agentVersion: "1",
    source: "hook",
    sourceSchema: "v1",
    reconstructionVersion: "1",
    policyRevision: "1",
    unit: "request",
    timing: "unknown",
    startNs: "1",
    endNs: "2",
    outcome: "incomplete",
    inputCompleteness: "unavailable",
    outputCompleteness: "unavailable",
  };
}
test("frozen HMAC/JCS identity matches every normative vector", () => {
  const turn = c.workId(
    key,
    vectors.namespace,
    "turn",
    vectors.session_anchor,
    vectors.turn_anchor,
  );
  assert.equal(turn, vectors.turn_work_id);
  assert.equal(c.traceId(turn), vectors.trace_id);
  for (const v of vectors.vectors) {
    const parts = v.canonical_preimage
      ? JSON.parse(v.canonical_preimage)
      : [
          "fi-work-v1",
          vectors.namespace,
          "request",
          vectors.session_anchor,
          vectors.turn_anchor,
          v.coordinate,
        ];
    const work = c.workId(
      key,
      parts[1],
      parts[2],
      parts[3],
      parts[4],
      parts[5],
    );
    assert.equal(work, v.source_work_id, v.case);
    assert.equal(c.spanId(work), v.span_id);
  }
});
test("identity refuses malformed Unicode, nonanchors, and missing reconstruction anchors", () => {
  for (const value of ["\ud800", "\udc00", 42, {}, ""])
    assert.throws(() => c.canonical(value));
  assert.throws(() => c.workId(key, "n", "request", ["s"], ["t"]));
  assert.throws(() => c.sessionId(Buffer.alloc(31), "n", ["s"]));
  assert.notEqual(
    c.workId(key, "n", "request", ["s"], ["t"], ["r"]),
    c.workId(key, "n", "request", ["s"], ["t"], ["r2"]),
  );
});
test("strong structure default rebuilds nested values instead of copying source fields", () => {
  const f = {
    ...fact(),
    prompt: "CANARY",
    content: { prompts: "CANARY", system: "CANARY" },
    attributes: { secret: "CANARY" },
    links: [
      { traceId: "a".repeat(32), spanId: "b".repeat(16), secret: "CANARY" },
    ],
  };
  const safe = c.minimize(f);
  assert.ok(!JSON.stringify(safe).includes("CANARY"));
  assert.equal(safe.content, undefined);
});
test("content ceiling, redaction and UTF-8 bounds are enforced before IPC", () => {
  const f = {
    ...fact(),
    content: {
      prompts: "api_key=CANARY person@example.com /Users/private/a",
      responses: "CANARY",
      reasoning: "CANARY",
    },
  };
  const safe = c.minimize(f, {
    revision: "2",
    categories: ["prompts", "responses"],
    ceiling: ["prompts"],
  });
  assert.ok(!JSON.stringify(safe).includes("CANARY"));
  assert.equal(safe.content.responses, undefined);
  assert.equal(safe.content.reasoning, undefined);
  f.content.prompts = "😀".repeat(32000);
  assert.ok(
    Buffer.byteLength(
      c.minimize(f, {
        revision: "2",
        categories: ["prompts"],
        ceiling: ["prompts"],
      }).content.prompts,
    ) <= 32768,
  );
});
test("usage normalization never doubles inclusive cache/reasoning; missing and resets stay unknown", () => {
  assert.deepEqual(
    c.normalizeUsage(
      { input: 100, output: 20, cacheRead: 40, reasoning: 8 },
      { cacheInInput: true, reasoningInOutput: true },
    ),
    { input: 100, output: 20, cacheRead: 40, reasoning: 8, total: 120 },
  );
  assert.equal(
    c.normalizeUsage(
      { input: 60, output: 12, cacheRead: 40, reasoning: 8 },
      { cacheInInput: false, reasoningInOutput: false },
    ).total,
    120,
  );
  assert.equal(
    c.normalizeUsage(
      { output: 20 },
      { cacheInInput: true, reasoningInOutput: true },
    ).total,
    undefined,
  );
  for (const args of [
    [undefined, 100, true],
    [100, 99, true],
    [100, 110, false],
  ])
    assert.throws(() => c.cumulativeDelta(...args));
  assert.equal(c.cumulativeDelta(100, 140, true), 40);
});
test("nonadditive summaries drop billable keys and malformed native IDs are rejected", () => {
  const f = fact();
  f.usage = {
    scope: "turn",
    contribution: "none",
    planId: "a".repeat(64),
    factIds: [],
    input: 42925,
    output: 146,
    inclusions: "inclusive",
    complete: true,
  };
  assert.equal(c.minimize(f).usage.input, undefined);
  assert.throws(() => c.minimize({ ...f, spanId: "0".repeat(16) }));
  assert.throws(() => c.minimize({ ...f, startNs: "3", endNs: "2" }));
});
test("unknown execution outcome is independent of capture completeness in runtime and JSON schemas", () => {
  const schema = require("../../../../integrations/coding-agents/schema/canonical-fact-v1.schema.json");
  const unknown = { ...fact(), outcome: "unknown" };
  assert.equal(c.minimize(unknown).outcome, "unknown");
  assert.equal(c.minimize(unknown).incomplete, undefined);
  assert.equal(c.minimize({ ...unknown, incomplete: true }).incomplete, true);
  assert.ok(schema.properties.outcome.enum.includes("unknown"));
  for (const outcome of schema.properties.outcome.enum)
    assert.doesNotThrow(() => c.validateFact({ ...fact(), outcome }));
  assert.throws(
    () => c.validateFact({ ...unknown, outcome: "invented" }),
    /INVALID_FACT/,
  );
});

test("missing runtime is neutral without creating any fallback file", async () => {
  const ack = await observe("/nonexistent-fi-agent/socket", "fixture", fact());
  assert.equal(ack.durable, false);
  assert.equal(ack.protocol_version, 1);
});
test("TypeScript and Python share every agent semantic key", () => {
  const root = path.resolve(__dirname, "../../../..");
  const attrs = require(
    path.join(root, "integrations/coding-agents/schema/attributes-v1.json"),
  );
  const python = fs.readFileSync(
    path.join(root, "python/fi_instrumentation/agent_constants.py"),
    "utf8",
  );
  const ts = require("@traceai/fi-semantic-conventions");
  for (const [name, value] of Object.entries(attrs)) {
    assert.equal(ts[name], value);
    assert.ok(python.includes(`${name} = "${value}"`));
  }
});
