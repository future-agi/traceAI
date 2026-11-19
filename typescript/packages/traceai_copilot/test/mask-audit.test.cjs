// Offline audit: Copilot's native adapter uses an allowlist plus its own
// hideInputs/hideOutputs gating (not fi-core maskAttributes). Keys fi-core now
// masks must never be exported, and hidden content must not reach span JSON.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { nativeSpan } = require("../dist/index.js");
const SECRET = "synthetic-mask-audit-secret";
const NEW_KEYS = [
  "tool.parameters", "tool.description", "tool_call.function.arguments",
  "message.function_call_arguments_json", "tool.json_schema", "gen_ai.agent.description",
  "gen_ai.prompts", "gen_ai.retrieval.query", "gen_ai.retrieval.documents.0.document.content",
  "reranker.query", "gen_ai.reranker.output_documents.0.document.content",
  "gen_ai.voice.transcript", "gen_ai.tool.parameters", "gen_ai.guardrail.modified_output",
  "gen_ai.evaluation.explanation",
];
const base = { captureContent: true, credentials: { apiKey: "k", secretKey: "s" },
  projectName: "p", hideInputs: false, hideOutputs: false, hideErrorDetails: false };
const raw = (operation) => ({
  type: "span", traceId: "a".repeat(32), spanId: "b".repeat(16), name: "n", kind: 2,
  startTime: [1, 0], endTime: [2, 0],
  attributes: {
    "gen_ai.operation.name": operation, "gen_ai.tool.name": "echo",
    ...Object.fromEntries(NEW_KEYS.map((k) => [k, SECRET])),
    "gen_ai.tool.definitions": JSON.stringify([{ name: SECRET }]),
    "gen_ai.tool.call.arguments": JSON.stringify({ q: SECRET }),
    "gen_ai.input.messages": JSON.stringify([{ role: "user", parts: [{ type: "text", content: SECRET }] }]),
    "gen_ai.system_instructions": SECRET,
    "gen_ai.tool.call.result": SECRET, "gen_ai.output.messages": SECRET,
  },
  status: { code: 1 }, events: [],
});
const dump = (span) => JSON.stringify({ a: span.attributes, e: span.events, s: span.status, n: span.name });

for (const op of ["chat", "execute_tool", "invoke_agent"]) {
  test(`${op}: newly-masked keys never exported (captureContent, hideInputs+hideOutputs)`, () => {
    const span = nativeSpan(raw(op), { ...base, hideInputs: true, hideOutputs: true });
    assert.ok(span);
    for (const k of NEW_KEYS) assert.equal(span.attributes[k], undefined, `${k} exported`);
    assert.ok(!dump(span).includes(SECRET), "secret present in exported span JSON");
  });
  test(`${op}: newly-masked keys never exported even with hiding disabled`, () => {
    const span = nativeSpan(raw(op), base);
    for (const k of NEW_KEYS) assert.equal(span.attributes[k], undefined, `${k} exported`);
  });
}
test("hideInputs alone drops tool definitions and arguments; hideOutputs alone drops results", () => {
  const i = nativeSpan(raw("execute_tool"), { ...base, hideInputs: true });
  for (const k of ["gen_ai.tool.definitions", "gen_ai.tool.call.arguments", "gen_ai.input.messages", "gen_ai.system_instructions"])
    assert.equal(i.attributes[k], undefined, k);
  const o = nativeSpan(raw("execute_tool"), { ...base, hideOutputs: true });
  for (const k of ["gen_ai.tool.call.result", "gen_ai.output.messages"]) assert.equal(o.attributes[k], undefined, k);
});
