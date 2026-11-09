// Real sanitized hooks; transcript rows are explicitly synthetic adaptations of the checked-in
// Cursor JSONL fixture. No user-home transcripts are copied or claimed as live qualification.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const load = (label) => require(`./fixtures/real-1106-${label}.json`);
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
const spans = (requests) =>
  requests.flatMap((r) =>
    (r.body?.resourceSpans ?? r.resourceSpans ?? []).flatMap((r) =>
      r.scopeSpans.flatMap((s) => s.spans),
    ),
  );
const toolsFor = (label) =>
  label === "run4"
    ? ["Grep", "Read", "Read", "Write", "Read", "Write", "Shell"]
    : ["Grep", "Read", "Read", "Write", "Shell"];
async function prepare(home, label, partial = false) {
  const file = path.join(
    home,
    ".cursor/projects/fixture/agent-transcripts",
    `${label}.jsonl`,
  );
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const text =
    label === "cli-print"
      ? ""
      : await fs.readFile(
          path.join(
            __dirname,
            `fixtures/synthetic-1106-${label}-transcript.jsonl`,
          ),
          "utf8",
        );
  const first = text.slice(0, text.indexOf("\n") + 1);
  await fs.writeFile(file, partial ? first : text, { mode: 0o600 });
  return { file, tail: partial ? text.slice(first.length) : "" };
}
function checkGolden(all, label) {
  const roots = all.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1);
  const root = roots[0],
    a = attrs(root),
    print = label === "cli-print";
  const tools = all.filter((s) => attrs(s)["fi.span.kind"] === "TOOL");
  assert.deepEqual(tools.map((s) => s.name).sort(), toolsFor(label).sort());
  const generations = all
    .filter((s) => attrs(s)["fi.span.kind"] === "LLM")
    .sort((a, b) =>
      Number(BigInt(a.startTimeUnixNano) - BigInt(b.startTimeUnixNano)),
    );
  assert.equal(generations.length, print ? 0 : 5);
  assert.equal(all.length, 1 + tools.length + generations.length);
  assert.equal(new Set(all.map((s) => s.spanId)).size, all.length);
  for (const s of all.filter((s) => s !== root)) {
    assert.equal(s.parentSpanId, root.spanId);
    assert.equal(s.traceId, root.traceId);
    assert.ok(BigInt(s.endTimeUnixNano) >= BigInt(s.startTimeUnixNano));
  }
  for (const s of tools) assert.equal(attrs(s)["tool.name"], s.name);
  const bearers = all.filter(
    (s) => attrs(s)["gen_ai.usage.input_tokens"] !== undefined,
  );
  if (print) {
    assert.equal(a["input.value"], undefined);
    assert.equal(a["fi.agent.gap"], "MISSING_PROMPT_AND_STOP");
    assert.equal(a["fi.agent.outcome"], "success");
    assert.equal(bearers.length, 0);
    assert.ok(
      all.every((s) =>
        Object.keys(attrs(s)).every(
          (k) =>
            !k.startsWith("gen_ai.usage.") &&
            !k.startsWith("fi.agent.summary."),
        ),
      ),
    );
  } else {
    const hooks = load(label),
      terminal = hooks.find((r) => r.hook_event_name === "stop");
    assert.equal(a["input.value"], hooks[0].prompt);
    assert.equal(bearers.length, 1);
    assert.equal(bearers[0].spanId, generations.at(-1).spanId);
    assert.equal(
      attrs(bearers[0])["output.value"],
      hooks.find((r) => r.hook_event_name === "afterAgentResponse").text,
    );
    for (const key of [
      "input_tokens",
      "output_tokens",
      "cache_read_tokens",
      "cache_write_tokens",
    ])
      assert.equal(
        Number(attrs(bearers[0])[`gen_ai.usage.${key}`]),
        terminal[key],
      );
    assert.equal(
      Number(attrs(bearers[0])["gen_ai.usage.total_tokens"]),
      terminal.input_tokens + terminal.output_tokens,
    );
    assert.equal(a["gen_ai.usage.total_tokens"], undefined);
    assert.equal(a["fi.usage.contribution"], "none");
    for (const [i, g] of generations.entries()) {
      const ga = attrs(g);
      assert.ok(ga["input.value"].includes(hooks[0].prompt));
      assert.ok(ga["output.value"].length > 0);
      assert.ok(ga["input.value"].length <= 20000);
      assert.ok(ga["output.value"].length <= 20000);
      assert.equal(ga["fi.agent.timing"], "reconstructed");
      assert.equal(ga["fi.agent.fidelity"], "reconstructed-message");
      if (i) {
        assert.ok(
          BigInt(g.startTimeUnixNano) >
            BigInt(generations[i - 1].startTimeUnixNano),
        );
        assert.ok(
          BigInt(g.endTimeUnixNano) >
            BigInt(generations[i - 1].endTimeUnixNano),
        );
        assert.match(ga["input.value"], /Assistant:.*|tool_calls/s);
      }
    }
    assert.ok(
      generations.some((g) =>
        attrs(g)["output.value"].startsWith('{"tool_calls":'),
      ),
    );
  }
  return {
    label,
    spanCount: all.length,
    tree: [
      "cursor [AGENT]",
      ...toolsFor(label).map((n) => `  ${n} [TOOL]`),
      ...generations.map(
        (g, i) =>
          `  assistant-message #${i + 1} [LLM]${g === bearers[0] ? ` tokens=${attrs(g)["gen_ai.usage.input_tokens"]}/${attrs(g)["gen_ai.usage.output_tokens"]} cache-read=${attrs(g)["gen_ai.usage.cache_read_tokens"]}` : ""}`,
      ),
    ],
    transcriptSource: print ? "none" : "synthetic-fixture-derived",
  };
}
module.exports = { load, attrs, spans, prepare, checkGolden };
