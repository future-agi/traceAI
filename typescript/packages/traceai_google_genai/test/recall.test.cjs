"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { createRequire } = require("node:module");
const consumer = process.env.GENAI_TEST_CONSUMER;
const consumerRequire = createRequire(path.join(consumer, "package.json"));
const latest =
  consumerRequire("@google/generative-ai/package.json").version !== "0.1.0";
function run(scenario, runner = "recall-cases.cjs") {
  const stdout = execFileSync(
    process.execPath,
    [path.join(__dirname, runner), consumer, scenario],
    { encoding: "utf8", timeout: 60000, env: { PATH: process.env.PATH } },
  );
  return JSON.parse(stdout.trim());
}
const S = {
  messages: "gen_ai.input.messages",
  parameters: "gen_ai.request.parameters",
  tools: "gen_ai.tool.definitions",
};
test("generation array overloads preserve successful real-SDK behavior", () => {
  const r = run("array-overloads");
  assert.deepEqual(r.on, r.off);
});
test("blocked responses are still returned for caller inspection", () => {
  const r = run("blocked-responses");
  assert.deepEqual(r.on, r.off);
  assert.ok(r.spans.every((s) => s.status.code === 1));
});
test("FI_HIDE_INPUTS also hides single and batch embedding text", () => {
  const r = run("privacy-embedding-input");
  assert.deepEqual(r.leaks, []);
});
test("FI_HIDE_EMBEDDING_VECTORS removes vectors from all exported copies", () => {
  const r = run("privacy-embedding-vector");
  assert.deepEqual(r.leaks, []);
});
test("FI_HIDE_INPUT_TEXT / OUTPUT_TEXT remove duplicate plaintext copies", () => {
  const r = run("privacy-text");
  assert.deepEqual(
    { input: r.inputLeaks, output: r.outputLeaks },
    { input: [], output: [] },
  );
});
test("FI_HIDE_INPUT_IMAGES removes raw inlineData payloads", () => {
  const r = run("privacy-image");
  assert.deepEqual(r.leaks, []);
});
test("generation metadata includes effective model-level configuration", () => {
  const r = run("model-defaults");
  assert.deepEqual(
    JSON.parse(r.attributes[S.parameters] || "null"),
    r.wire.generationConfig,
  );
  if (latest) {
    assert.ok(
      r.attributes[S.messages].includes("synthetic-system-instruction"),
    );
    assert.ok(r.attributes[S.tools].includes("get_weather"));
  }
});
if (latest) {
  test("string systemInstruction appears in input messages", () => {
    const r = run("request-system-string");
    assert.ok(
      r.attributes[S.messages].includes("synthetic-system-instruction"),
    );
  });
  test("response model uses the returned modelVersion, not the request alias", () => {
    const r = run("response-model");
    assert.equal(r.attribute, r.responseModel);
  });
}
test("embedding array and multi-part text are not silently truncated", () => {
  const r = run("embedding-overloads");
  for (const a of r.attributes)
    assert.equal(
      a["embedding.embeddings.0.embedding.text"],
      "synthetic-firstsynthetic-second",
    );
});
test("disable reverses the documented manual installation path", () => {
  const r = run("disable-manual");
  assert.deepEqual(r, {
    sdkVersion: r.sdkVersion,
    scenario: r.scenario,
    restored: true,
    patched: false,
    enabled: false,
    spanCount: 1,
  });
});
test("one instrumentation instance can cover two loaded SDK copies", () => {
  const r = run("multiple-sdk-copies");
  assert.equal(r.spanCount, r.requests);
});
test("parenting, suppression and duplicate manual setup remain correct", () => {
  const r = run("parent-suppression-duplicate");
  assert.equal(r.spans.length, 2);
  const child = r.spans.find((s) => s.name !== "parent");
  const parent = r.spans.find((s) => s.name === "parent");
  assert.equal(child.parent.spanId, parent.id);
  assert.equal(r.contexts[0], child.id);
});
if (latest)
  test("aborted and timed-out SDK calls reject and export ERROR", () => {
    const r = run("cancel-timeout");
    assert.equal(r.cancelled.resolved, false);
    assert.equal(r.timedout.resolved, false);
    assert.equal(r.spans.length, 2);
    assert.ok(r.spans.every((s) => s.status.code === 2));
  });
test("FI_HIDE_ERROR_DETAILS removes error content but preserves failure", () => {
  const r = run("hidden-errors");
  assert.equal(r.result.resolved, false);
  assert.equal(r.spans[0].status.code, 2);
  assert.ok(
    !JSON.stringify(r.spans).includes("synthetic-sensitive-error-PRIVATE"),
  );
});

test("all privacy options protect every copy without changing SDK payloads", () => {
  run("privacy-options");
});
test("per-call config replaces defaults without mutating them", () => {
  run("model-overrides");
});
test("missing response model is not fabricated from the request alias", () => {
  run("missing-response-model");
});
test("manual lifecycle is reversible and respects prototype ownership", () => {
  run("lifecycle-ownership");
});
test("telemetry extraction cannot alter results, arguments or errors", () => {
  run("extraction-failures");
});

test("automatic and manual installation coexist without duplicate spans", () => {
  run("auto-lifecycle");
});
test("packed ESM adapter reversibly patches immutable SDK namespaces", () => {
  run("esm-lifecycle");
});

for (const scenario of [
  "credential-errors",
  "credential-copies",
  "privacy-combinations",
  "privacy-environment",
  "pii-span-lifecycle",
  ...(latest
    ? [
        "credential-header",
        "custom-header-credentials",
        "custom-header-transports",
      ]
    : []),
]) {
  test(`security regression: ${scenario}`, () => {
    run(scenario, "security-cases.cjs");
  });
}
