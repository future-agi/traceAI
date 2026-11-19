const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const otel = require("@opentelemetry/api");
const c = require("../../../dist/src/index.js");
async function fixture(t, respond) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-tracer-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    const data = [];
    req.on("data", (b) => data.push(b));
    req.on("end", () => {
      const body = JSON.parse(Buffer.concat(data).toString());
      requests.push({ body, headers: req.headers });
      res.setHeader("Content-Type", "application/json");
      if (!body.resourceSpans.length) res.end("{}");
      else if (respond) respond(body, res);
      else res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const options = { agent: "pi", home, env: {} };
  await c.setup({
    ...options,
    credentials: { apiKey: "test-api", secretKey: "test-secret" },
    projectName: "traces",
    otlpEndpoint: endpoint,
    configureOnly: true,
  });
  const config = await c.resolveConfig(options);
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  return { home, requests, options, config };
}
function spans(f) {
  return f.requests.flatMap((r) =>
    r.body.resourceSpans.flatMap((rs) =>
      rs.scopeSpans.flatMap((ss) => ss.spans),
    ),
  );
}
function attrs(span) {
  return Object.fromEntries(
    (span.attributes || []).map((a) => [a.key, Object.values(a.value)[0]]),
  );
}
const usage = {
  input: 100,
  output: 20,
  cacheRead: 40,
  reasoning: 8,
  cacheInInput: true,
  reasoningInOutput: true,
  costUsd: 0.01,
};
test("two prompt roots, same session; requests/tools/subagent parentage; additive usage once; isolated provider", async (t) => {
  const f = await fixture(t),
    globalBefore = otel.trace.getTracerProvider(),
    managerContext = otel.context.active();
  const tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({
    sessionId: "session",
    turnId: "turn-1",
    input: "Read a public file",
    usagePlan: "request",
  });
  turn
    .child({
      id: "r1",
      kind: "llm",
      model: "test-model",
      provider: "mock",
      fidelity: "native-request",
      input: "model input",
    })
    .end({ output: "tool request", usage });
  const tool = turn.child({
    id: "call-1",
    kind: "tool",
    name: "read_file",
    input: "public.txt",
  });
  const child = turn.child({
    id: "sub-1",
    kind: "agent",
    parentId: "call-1",
    name: "subagent",
  });
  child.end({ output: "done" });
  tool.end({ output: "hello" });
  turn
    .child({
      id: "r2",
      kind: "llm",
      model: "test-model",
      fidelity: "native-request",
    })
    .end({ output: "answer", usage: { ...usage, input: 20, output: 10 } });
  turn.child({ id: "compact", kind: "compaction" }).end();
  turn.end({ output: "done", usage: { ...usage, input: 120, output: 30 } });
  tracer
    .startTurn({ sessionId: "session", turnId: "turn-2", input: "next prompt" })
    .end({ outcome: "interrupted" });
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(otel.trace.getTracerProvider(), globalBefore);
  assert.equal(otel.context.active(), managerContext);
  const all = spans(f),
    roots = all.filter((s) => !s.parentSpanId),
    llms = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(roots.length, 2);
  assert.equal(llms.length, 2);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.equal(attrs(roots[0])["session.id"], attrs(roots[1])["session.id"]);
  assert.equal(attrs(roots[0])["gen_ai.usage.input_tokens"], undefined);
  assert.equal(Number(attrs(roots[0])["fi.agent.summary.input"]), 120);
  assert.equal(
    llms.reduce((n, s) => n + Number(attrs(s)["gen_ai.usage.input_tokens"]), 0),
    120,
  );
  assert.equal(
    all.find((s) => s.name === "subagent").parentSpanId,
    all.find((s) => s.name === "read_file").spanId,
  );
  assert.equal(attrs(roots[1])["fi.agent.outcome"], "interrupted");
  for (const r of f.requests.filter((r) => r.body.resourceSpans.length)) {
    assert.equal(r.headers["x-project-name"], "traces");
    const resource = attrs(r.body.resourceSpans[0].resource);
    assert.equal(resource.project_type, "observe");
    assert.equal(resource.project_name, "traces");
    assert.ok(
      !Object.keys(resource).some(
        (k) => k.startsWith("host.") || k.startsWith("process."),
      ),
    );
  }
});
test("deterministic finalization, final turn flush without next prompt, no synthetic model requests", async (t) => {
  const f = await fixture(t),
    tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: "s", turnId: "t" });
  turn
    .child({
      id: "reconstructed",
      kind: "llm",
      fidelity: "reconstructed-message",
    })
    .end({ usage });
  turn.end();
  turn.end();
  tracer.startTurn({ sessionId: "s", turnId: "t" }).end();
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(spans(f).length, 2);
  assert.equal(spans(f).filter((s) => !s.parentSpanId).length, 1);
  assert.equal(
    spans(f).filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    0,
  );
  assert.ok(
    spans(f).every((s) => attrs(s)["gen_ai.usage.input_tokens"] === undefined),
  );
});
test("missing child stop produces incomplete diagnostics, never silence-based success", async (t) => {
  const f = await fixture(t),
    tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: "s", turnId: "t" });
  turn.child({ id: "tool", kind: "tool" });
  turn.end();
  await tracer.shutdown();
  assert.ok(
    spans(f).every((s) => attrs(s)["fi.agent.outcome"] === "incomplete"),
  );
  assert.equal(
    attrs(spans(f).find((s) => !s.parentSpanId))["fi.agent.gap"],
    "MISSING_CHILD_STOP",
  );
});
test("content opt-out drops inputs/outputs while preserving structure", async (t) => {
  const f = await fixture(t);
  const config = await c.resolveConfig({
    ...f.options,
    env: { FI_AGENT_CAPTURE_CONTENT: "false" },
  });
  const tracer = c.createTurnTracer({ config });
  const turn = tracer.startTurn({
    sessionId: "s",
    turnId: "t",
    input: "PRIVATE PROMPT",
  });
  turn
    .child({ id: "tool", kind: "tool", input: "PRIVATE TOOL" })
    .end({ output: "PRIVATE OUTPUT", outcome: "error" });
  turn.end({ output: "PRIVATE ANSWER" });
  await tracer.shutdown();
  assert.equal(spans(f).length, 2);
  assert.ok(!JSON.stringify(f.requests).includes("PRIVATE"));
});
test("known credentials are redacted and content is capped", async (t) => {
  const f = await fixture(t),
    tracer = c.createTurnTracer({ config: f.config });
  tracer
    .startTurn({
      sessionId: "s",
      turnId: "t",
      input: "api_key=sk-dummy-secret-long " + "x".repeat(40000),
    })
    .end();
  await tracer.shutdown();
  const text = attrs(spans(f)[0])["input.value"];
  assert.ok(text.length <= 20000);
  assert.ok(!text.includes("sk-dummy-secret-long"));
});
test("413 splits, partial success never retries accepted members, failures remain metadata-only", async (t) => {
  let requests = 0;
  const f = await fixture(t, (body, res) => {
    requests++;
    const count = body.resourceSpans.flatMap((r) =>
      r.scopeSpans.flatMap((s) => s.spans),
    ).length;
    if (count > 2) {
      res.statusCode = 413;
      res.end("{}");
    } else
      res.end(
        '{"partialSuccess":{"rejectedSpans":"1","errorMessage":"PRIVATE_SERVER_BODY"}}',
      );
  });
  const tracer = c.createTurnTracer({ config: f.config });
  for (let i = 0; i < 4; i++)
    tracer.startTurn({ sessionId: "s", turnId: `t${i}` }).end();
  await tracer.shutdown();
  assert.equal(requests, 3);
  assert.equal(tracer.state.exported, 2);
  assert.equal(tracer.state.dropped, 2);
  const status = await c.inspectStatus(f.options);
  assert.equal(status.delivery.lastError, "PARTIAL_REJECTION");
  assert.ok(!JSON.stringify(status).includes("PRIVATE_SERVER_BODY"));
});
test("bounded queue drops excess and transport failure never throws into agent", async (t) => {
  const f = await fixture(t, (_body, res) => {
    res.statusCode = 429;
    res.end("sensitive text");
  });
  const tracer = c.createTurnTracer({ config: f.config });
  for (let i = 0; i < 300; i++)
    tracer.startTurn({ sessionId: "s", turnId: `turn-${i}` }).end();
  assert.ok(tracer.state.pending <= 256);
  assert.ok(tracer.state.dropped >= 44);
  await tracer.shutdown();
  assert.equal(tracer.state.lastError, "RATE_LIMITED");
  assert.equal(tracer.state.pending, 0);
});
test("queued spans retain their original destination when caller changes configuration", async (t) => {
  const f = await fixture(t),
    tracer = c.createTurnTracer({ config: f.config });
  tracer
    .startTurn({
      sessionId: "s",
      turnId: "t",
      input: "test-secret without a label",
    })
    .end();
  f.config.endpoint = "http://127.0.0.1:1/not-the-approved-destination";
  await tracer.shutdown();
  assert.equal(spans(f).length, 1);
  assert.ok(!attrs(spans(f)[0])["input.value"].includes("test-secret"));
});
test("error-detail privacy suppresses error outputs even with content capture enabled", async (t) => {
  const f = await fixture(t),
    config = await c.resolveConfig({
      ...f.options,
      env: { FI_HIDE_ERROR_DETAILS: "true" },
    });
  const tracer = c.createTurnTracer({ config });
  const turn = tracer.startTurn({
    sessionId: "s",
    turnId: "t",
    input: "allowed input",
  });
  turn
    .child({ id: "tool", kind: "tool" })
    .end({ outcome: "error", output: "PRIVATE ERROR BODY" });
  turn.end({ outcome: "error", output: "PRIVATE ERROR BODY" });
  await tracer.shutdown();
  assert.equal(spans(f).length, 2);
  assert.ok(!JSON.stringify(f.requests).includes("PRIVATE ERROR BODY"));
});

test("shutdown drains the final turn root queued during a child export", async (t) => {
  let acknowledge, received;
  const firstRequest = new Promise((resolve) => {
    received = resolve;
  });
  let count = 0;
  const f = await fixture(t, (_body, res) => {
    if (++count === 1) {
      acknowledge = () => res.end("{}");
      received();
    } else res.end("{}");
  });
  const tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: "s", turnId: "pending-root" });
  turn.child({ id: "request", kind: "llm", fidelity: "native-request" }).end();
  const flush = tracer.flush();
  await firstRequest;
  turn.end();
  const shutdown = tracer.shutdown();
  acknowledge();
  await Promise.all([flush, shutdown]);
  assert.equal(spans(f).length, 2);
  assert.equal(spans(f).filter((s) => !s.parentSpanId).length, 1);
  assert.equal(tracer.state.exported, 2);
  assert.equal(tracer.state.dropped, 0);
  assert.equal(tracer.state.pending, 0);
});

test("shutdown drains more than one batch without giving each request a fresh budget", async (t) => {
  const f = await fixture(t);
  const tracer = c.createTurnTracer({ config: f.config });
  for (let i = 0; i < 130; i++)
    tracer.startTurn({ sessionId: "s", turnId: `batch-${i}` }).end();
  await tracer.shutdown();
  assert.equal(tracer.state.exported, 130);
  assert.equal(tracer.state.dropped, 0);
  assert.equal(tracer.state.pending, 0);
});

test("shutdown's five-second budget includes the in-flight request", async (t) => {
  let received;
  const firstRequest = new Promise((resolve) => {
    received = resolve;
  });
  let count = 0;
  const f = await fixture(t, (_body, res) => {
    if (++count === 1) {
      setTimeout(() => res.end("{}"), 2500);
      received();
    }
    // The final batch never acknowledges: it gets only the remaining budget.
  });
  const tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: "s", turnId: "deadline-root" });
  turn.child({ id: "request", kind: "llm", fidelity: "native-request" }).end();
  const flush = tracer.flush();
  await firstRequest;
  turn.end();
  const started = Date.now();
  await tracer.shutdown();
  await flush;
  assert.ok(Date.now() - started < 6500);
  assert.equal(count, 2);
  assert.equal(tracer.state.exported, 1);
  assert.equal(tracer.state.dropped, 1);
  assert.equal(tracer.state.pending, 0);
});

test("concurrent processor shutdown calls share the same bounded drain", async (t) => {
  const f = await fixture(t);
  const processor = new c.BoundedSpanProcessor(f.config);
  const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
  const provider = new BasicTracerProvider({ spanProcessors: [processor] });
  for (let i = 0; i < 130; i++)
    provider.getTracer("shutdown-regression").startSpan(`span-${i}`).end();
  const first = processor.shutdown();
  assert.equal(processor.shutdown(), first);
  await first;
  assert.equal(processor.state.exported, 130);
  assert.equal(processor.state.pending, 0);
  assert.equal(processor.state.dropped, 0);
  await provider.shutdown();
});
