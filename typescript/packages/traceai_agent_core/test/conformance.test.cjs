const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  checkShape,
  checkPrivacy,
  MARKERS,
} = require("./conformance/index.cjs");
const attrs = (value) =>
  Object.entries(value).map(([key, v]) => ({
    key,
    value: typeof v === "string" ? { stringValue: v } : { intValue: v },
  }));
function valid() {
  return [
    {
      resourceSpans: [
        {
          scopeSpans: [
            {
              spans: [
                {
                  traceId: "a",
                  spanId: "r",
                  attributes: attrs({
                    "session.id": "s",
                    "fi.agent.outcome": "success",
                    "input.value": MARKERS.input,
                  }),
                },
                {
                  traceId: "a",
                  spanId: "m",
                  parentSpanId: "r",
                  attributes: attrs({
                    "fi.span.kind": "LLM",
                    "gen_ai.request.model": "test",
                    "gen_ai.usage.input_tokens": 10,
                    "gen_ai.usage.output_tokens": 4,
                    "output.value": MARKERS.output,
                  }),
                },
                {
                  traceId: "a",
                  spanId: "t",
                  parentSpanId: "m",
                  attributes: attrs({
                    "fi.span.kind": "TOOL",
                    "tool.name": "read",
                    "output.value": MARKERS.tool,
                  }),
                },
              ],
            },
          ],
        },
      ],
    },
  ];
}
const all = (b) => b[0].resourceSpans[0].scopeSpans[0].spans;
test("contract accepts valid OTLP and does not mutate it", () => {
  const b = valid(),
    original = structuredClone(b);
  checkShape(b, { calls: 1, tools: 1 });
  checkPrivacy(b, "default");
  assert.deepEqual(b, original);
});
for (const [name, mutate] of [
  ["missing root", (b) => all(b).shift()],
  ["lost tool", (b) => all(b).pop()],
  ["duplicate spans", (b) => all(b).push(all(b)[2])],
  ["orphan", (b) => (all(b)[2].parentSpanId = "missing")],
  [
    "double-counted tokens",
    (b) =>
      all(b)[0].attributes.push(...attrs({ "gen_ai.usage.input_tokens": 10 })),
  ],
  [
    "wrong tokens",
    (b) =>
      (all(b)[1].attributes.find(
        (a) => a.key === "gen_ai.usage.output_tokens",
      ).value.intValue = 7),
  ],
  ["missing tool name", (b) => (all(b)[2].attributes = [])],
  [
    "wrong outcome",
    (b) =>
      (all(b)[0].attributes.find(
        (a) => a.key === "fi.agent.outcome",
      ).value.stringValue = "cancelled"),
  ],
])
  test("contract rejects " + name, () => {
    const b = valid();
    mutate(b);
    assert.throws(() => checkShape(b, { calls: 1, tools: 1 }));
  });
for (const location of ["resource", "event", "context"])
  test(
    "privacy finds content outside top-level span output: " + location,
    () => {
      const b = valid();
      if (location === "resource")
        b[0].resourceSpans[0].resource = {
          attributes: attrs({ private: MARKERS.secret }),
        };
      if (location === "event") all(b)[0].events = [{ name: MARKERS.secret }];
      if (location === "context")
        all(b)[1].attributes.push(
          ...attrs({
            "input.value": JSON.stringify({
              role: "tool",
              content: MARKERS.tool,
            }),
          }),
        );
      assert.throws(() =>
        checkPrivacy(b, location === "context" ? "FI_HIDE_OUTPUTS" : "default"),
      );
    },
  );
test("empty exports cannot pass privacy", () =>
  assert.throws(() => checkPrivacy([], "off")));
test("test helper is a separate opt-in export", () => {
  const manifest = require("../package.json");
  assert(manifest.exports["./test/conformance"]);
  assert(!("runConformance" in require("../dist/src/index.js")));
  assert.equal(
    require("@traceai/agent-core/test/conformance").checkShape,
    checkShape,
  );
});

test("root-only additive usage is accepted and non-additive summaries do not substitute", () => {
  const b = valid();
  all(b).splice(1);
  all(b)[0].attributes.push(
    ...attrs({
      "gen_ai.usage.input_tokens": 20,
      "gen_ai.usage.output_tokens": 8,
      "fi.usage.contribution": "additive",
    }),
  );
  checkShape(b, { calls: 0, tools: 0, inputTokens: 20, outputTokens: 8 });
  all(b)[0].attributes = attrs({
    "session.id": "s",
    "fi.agent.outcome": "success",
    "fi.agent.summary.input_tokens": 20,
  });
  assert.throws(() =>
    checkShape(b, { calls: 0, tools: 0, inputTokens: 20, outputTokens: 8 }),
  );
});
