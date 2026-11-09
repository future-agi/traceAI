const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const core = require("@traceai/agent-core");
const { SemanticConventions: SC } = require("@traceai/fi-semantic-conventions");
const { NativeSpans, runCopilot } = require("../dist/index.js");

const config = {
  captureContent: true,
  credentials: { apiKey: "synthetic-api", secretKey: "synthetic-secret" },
  projectName: "copilot-contract",
};
const rootId = "a".repeat(16);
const childId = "b".repeat(16);
const traceId = "c".repeat(32);
function record(operation, extra = {}, fields = {}) {
  return {
    type: "span", traceId,
    spanId: operation === "invoke_agent" ? rootId : childId,
    parentSpanId: operation === "invoke_agent" ? undefined : rootId,
    startTime: [1, 0], endTime: [2, 0], status: { code: 0 },
    attributes: {
      "gen_ai.operation.name": operation,
      "gen_ai.conversation.id": "contract-session",
      ...extra,
    },
    ...fields,
  };
}
const usage = {
  "gen_ai.usage.input_tokens": 20,
  "gen_ai.usage.output_tokens": 8,
  "gen_ai.usage.total_tokens": 28,
  "gen_ai.usage.cache_read.input_tokens": 5,
  "gen_ai.usage.cache_write.input_tokens": 2,
  "gen_ai.usage.reasoning.output_tokens": 3,
  "gen_ai.usage.cost_usd": 0.02,
  "github.copilot.cost": 3,
};

test("native terminal roots always label success/error independently of native status defaults", () => {
  for (const code of [0, 1, 2]) {
    const mapper = new NativeSpans(config);
    const root = mapper.accept(record("invoke_agent", {}, { status: { code } }));
    assert.equal(root.attributes[SC.FI_AGENT_OUTCOME], code === 2 ? "error" : "success");
    assert.equal(root.status.code, code);
    assert.deepEqual(mapper.end(), []);
  }
});

test("a native root abort remains interrupted even when its final request failed", () => {
  const mapper = new NativeSpans(config);
  mapper.accept(record("chat", usage, { status: { code: 2 } }));
  const root = mapper.accept(record("invoke_agent", usage, { events: [{
    name: "github.copilot.session.abort",
    attributes: { "github.copilot.abort_reason": "user_initiated" },
  }] }));
  assert.equal(root.attributes[SC.FI_AGENT_OUTCOME], "interrupted");
  assert.equal(root.status.code, 2);
});

test("root-only provider usage is additive with canonical token/cache/reasoning/cost aliases", () => {
  for (const captureContent of [true, false]) {
    const mapper = new NativeSpans({ ...config, captureContent });
    // Tools have no request usage and must not suppress the root fallback.
    mapper.accept(record("execute_tool", { "gen_ai.tool.name": "read" }));
    const root = mapper.accept(record("invoke_agent", usage));
    const a = root.attributes;
    assert.equal(a[SC.FI_USAGE_CONTRIBUTION], "additive");
    assert.equal(a[SC.FI_USAGE_SCOPE], "turn");
    assert.equal(a[SC.FI_AGENT_USAGE_PLAN], "turn");
    assert.equal(a[SC.LLM_TOKEN_COUNT_PROMPT], 20);
    assert.equal(a[SC.LLM_TOKEN_COUNT_COMPLETION], 8);
    assert.equal(a[SC.LLM_TOKEN_COUNT_TOTAL], 28);
    assert.equal(a[SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ], 5);
    assert.equal(a[SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE], 2);
    assert.equal(a[SC.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING], 3);
    assert.equal(a[SC.GEN_AI_COST_TOTAL], 0.02);
    assert.equal(a[SC.FI_COST_KIND], "provider_reported");
    assert.equal(a["github.copilot.cost"], undefined);
    assert.equal(a["fi.agent.aggregate.github.copilot.cost"], 3);
    assert.equal(a["fi.agent.aggregate.gen_ai.usage.input_tokens"], 20);
    assert.equal(a[SC.FI_AGENT_OUTCOME], "success");
    assert.deepEqual(mapper.end(), []);
  }
});

test("missing usage stays unknown and reported zeros remain additive", () => {
  const mapper = new NativeSpans(config);
  const unknown = mapper.accept(record("invoke_agent", { "github.copilot.cost": 3 }));
  assert.equal(unknown.attributes[SC.LLM_TOKEN_COUNT_TOTAL], undefined);
  assert.equal(unknown.attributes[SC.GEN_AI_COST_TOTAL], undefined);
  assert.equal(unknown.attributes[SC.FI_USAGE_CONTRIBUTION], "none");
  const zero = mapper.accept(record("invoke_agent", {
    "gen_ai.usage.input_tokens": 0, "gen_ai.usage.output_tokens": 0,
  }));
  assert.equal(zero.attributes[SC.LLM_TOKEN_COUNT_TOTAL], 0);
  assert.equal(zero.attributes[SC.FI_USAGE_CONTRIBUTION], "additive");
});

test("descendant usage is owned once through nested agents and isolated by trace", () => {
  const mapper = new NativeSpans(config);
  const nestedId = "d".repeat(16);
  const child = mapper.accept(record("chat", usage, { parentSpanId: nestedId }));
  const nested = mapper.accept(record("invoke_agent", usage, {
    spanId: nestedId, parentSpanId: rootId,
  }));
  const unrelated = mapper.accept(record("invoke_agent", usage, { traceId: "e".repeat(32) }));
  const root = mapper.accept(record("invoke_agent", usage));
  for (const span of [root, nested]) {
    assert.equal(span.attributes[SC.LLM_TOKEN_COUNT_PROMPT], undefined);
    assert.equal(span.attributes["gen_ai.usage.input_tokens"], undefined);
    assert.equal(span.attributes[SC.GEN_AI_COST_TOTAL], undefined);
    assert.equal(span.attributes[SC.FI_AGENT_OUTCOME], "success");
  }
  assert.equal(child.attributes[SC.LLM_TOKEN_COUNT_PROMPT], 20);
  assert.equal(unrelated.attributes[SC.LLM_TOKEN_COUNT_PROMPT], 20);
  assert.deepEqual(mapper.end(), []);
});

test("EOF recovers one incomplete structural parent without inventing content, usage or outcome", () => {
  for (const policy of [{}, { captureContent: false }, { hideInputs: true },
    { hideOutputs: true }, { hideErrorDetails: true }]) {
    const mapper = new NativeSpans({ ...config, ...policy });
    mapper.accept(record("chat", {
      ...usage, "gen_ai.input.messages": "PRIVATE_PROMPT", "gen_ai.output.messages": "PRIVATE_OUTPUT",
    }, { status: { code: 2 }, startTime: [3, 0], endTime: [4, 0], events: [{
      name: "github.copilot.session.abort",
      attributes: { "github.copilot.abort_reason": "user_initiated" },
    }] }));
    mapper.accept(record("execute_tool", { "gen_ai.tool.name": "read" }, {
      spanId: "f".repeat(16), startTime: [1, 500], endTime: [2, 0],
    }));
    const [root, extra] = mapper.end();
    assert.equal(extra, undefined);
    assert.equal(root.spanContext().traceId, traceId);
    assert.equal(root.spanContext().spanId, rootId);
    assert.equal(root.parentSpanContext, undefined);
    assert.equal(root.attributes["session.id"], "contract-session");
    assert.equal(root.attributes[SC.FI_AGENT_OUTCOME], "incomplete");
    assert.equal(root.attributes["fi.agent.source"], "reconstructed");
    assert.equal(root.attributes["fi.agent.gap"], "NATIVE_TERMINAL_RECORD_MISSING");
    assert.equal(root.attributes[SC.FI_AGENT_FIDELITY], "unknown");
    assert.equal(root.attributes[SC.FI_AGENT_TIMING], "reconstructed");
    assert.equal(root.attributes[SC.FI_AGENT_USAGE_PLAN], "unknown");
    assert.equal(root.status.code, 0);
    assert.equal(root.attributes[SC.LLM_TOKEN_COUNT_TOTAL], undefined);
    assert.equal(root.attributes[SC.FI_USAGE_CONTRIBUTION], "none");
    assert.deepEqual(root.startTime, [1, 500]);
    assert.deepEqual(root.endTime, [4, 0]);
    assert.ok(!JSON.stringify(root).includes("PRIVATE_"));
    assert.deepEqual(mapper.end(), [], "EOF is idempotent");
  }
});

test("late or already observed parents are not reconstructed a second time", () => {
  for (const rootFirst of [false, true]) {
    const mapper = new NativeSpans(config);
    if (rootFirst) mapper.accept(record("invoke_agent"));
    mapper.accept(record("chat", usage));
    if (!rootFirst) mapper.accept(record("invoke_agent", usage));
    assert.deepEqual(mapper.end(), []);
  }
});

test("launcher closes the native FIFO with an incomplete root when the host omits its terminal record", async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "copilot-eof-"));
  const payloads = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      payloads.push(JSON.parse(Buffer.concat(chunks)));
      res.setHeader("content-type", "application/json");
      res.end("{}");
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const oldPath = process.env.PATH;
  t.after(async () => {
    if (oldPath === undefined) delete process.env.PATH;
    else process.env.PATH = oldPath;
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await fs.rm(home, { recursive: true, force: true });
  });
  const bin = path.join(home, "bin");
  await fs.mkdir(bin, { mode: 0o700 });
  const native = record("chat", usage);
  await fs.writeFile(path.join(bin, "copilot"), `#!${process.execPath}
if (process.argv.includes('--version')) {
  console.log('GitHub Copilot CLI 1.0.106.');
} else {
  require('node:fs').writeFileSync(process.env.COPILOT_OTEL_FILE_EXPORTER_PATH, ${JSON.stringify(JSON.stringify(native) + "\n")});
}
`, { mode: 0o700 });
  process.env.PATH = bin + path.delimiter + oldPath;
  const options = { agent: "copilot", home, env: {} };
  await core.setup({ ...options, projectName: "copilot-contract",
    credentials: config.credentials, configureOnly: true,
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
  });
  const resolved = await core.resolveConfig(options);
  assert.equal(await runCopilot({ config: resolved }, []), 0);
  const spans = payloads.flatMap(p => p.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.length, 2);
  const roots = spans.filter(s => !s.parentSpanId);
  assert.equal(roots.length, 1);
  const attributes = Object.fromEntries(roots[0].attributes.map(a => [a.key, a.value.stringValue]));
  assert.equal(attributes[SC.FI_AGENT_OUTCOME], "incomplete");
  assert.equal(roots[0].spanId, rootId);
  assert.equal(spans.find(s => s.parentSpanId).parentSpanId, rootId);
});
