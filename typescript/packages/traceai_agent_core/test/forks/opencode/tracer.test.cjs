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
  assert.equal(attrs(all.find(s => s.name === "read_file"))["tool.name"], "read_file");
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
test('shared sanitizer covers quoted and escaped JSON credential assignments at the exporter', async t => {
  const f = await fixture(t), tracer = c.createTurnTracer({ config: f.config });
  const credentials = { access_token: 'SHARED_ACCESS_SENTINEL', refreshToken: 'SHARED_REFRESH_SENTINEL',
    api_key: 'SHARED_API_SENTINEL', clientSecret: 'SHARED_CLIENT_SENTINEL', safe: 'visible result' };
  const values = [JSON.stringify(credentials), JSON.stringify({ text: JSON.stringify(credentials) }),
    'access_token=SHARED_ACCESS_SENTINEL refreshToken=SHARED_REFRESH_SENTINEL api_key=SHARED_API_SENTINEL'];
  for (const [i, input] of values.entries()) tracer.startTurn({ sessionId: 's', turnId: `json-${i}`, input }).end();
  await tracer.shutdown();
  const all = spans(f), serialized = JSON.stringify(all);
  assert.equal(all.length, values.length);
  assert.ok(!serialized.includes('SHARED_'));
  const json = all.filter(s => attrs(s)['input.value'].startsWith('{'));
  assert.equal(json.length, 2);
  for (const span of json) assert.doesNotThrow(() => JSON.parse(attrs(span)['input.value']));
  assert.ok(serialized.includes('visible result'));
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
test("catalog-derived host costs are labeled estimates rather than provider billing", async (t) => {
  const f = await fixture(t);
  const tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: "cost-session", turnId: "cost-turn" });
  turn.child({ id: "estimated", kind: "llm", fidelity: "native-request" }).end({
    usage: { input: 10, output: 5, cacheInInput: false, reasoningInOutput: false,
      costUsd: 0.001, costSource: "estimated", costEstimator: "opencode-2.2.5-catalog" },
  });
  turn.end();
  await tracer.flush();
  const a = attrs(spans(f).find((s) => attrs(s)["fi.span.kind"] === "LLM"));
  assert.equal(a["gen_ai.cost.total"], 0.001);
  assert.equal(a["gen_ai.usage.cost"], undefined);
  assert.equal(a["fi.agent.cost.source"], "estimated");
  assert.equal(a["fi.agent.cost.estimator"], "opencode-2.2.5-catalog");
  await tracer.shutdown();
});

test('delivery evidence belongs to its destination, project and enable generation', async t => {
  const f = await fixture(t), tracer = c.createTurnTracer({ config: f.config });
  tracer.startTurn({ sessionId: 's', turnId: 'first' }).end();
  await tracer.shutdown();
  const first = await c.inspectStatus(f.options);
  assert.equal(first.activation, 'OBSERVED'); assert.equal(first.delivery.exported, 1);
  for (const env of [{ FI_PROJECT_NAME: 'second' }, { FI_OTLP_ENDPOINT: f.config.endpoint + '/second' }]) {
    const next = await c.inspectStatus({ ...f.options, env: { ...env, FI_API_KEY: 'test-api', FI_SECRET_KEY: 'test-secret' } });
    assert.equal(next.activation, 'NOT_OBSERVED'); assert.equal(next.delivery.exported, 0);
    assert.equal(next.delivery.lastAcceptedAt, undefined);
  }
  await c.disable(f.options); await c.enable(f.options);
  assert.equal((await c.inspectStatus(f.options)).activation, 'NOT_OBSERVED');
});

test('exclusive compaction usage is additive once without pretending to be an LLM request', async t => {
  const f = await fixture(t), tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: 's', turnId: 'compact', usagePlan: 'attempt' });
  turn.child({ id: 'request', kind: 'llm', fidelity: 'native-request' }).end({ usage });
  const compact = turn.child({ id: 'compact', kind: 'compaction', fidelity: 'aggregate-turn', usageContribution: 'exclusive' });
  compact.end({ usage }); compact.end({ usage });
  // Default/overlapping summaries remain non-additive; opt-in cannot make tools additive.
  turn.child({ id: 'summary', kind: 'compaction', fidelity: 'aggregate-turn' }).end({ usage });
  turn.child({ id: 'tool', kind: 'tool', usageContribution: 'exclusive' }).end({ usage });
  turn.end({ usage });
  for (const plan of ['turn', 'unknown']) {
    const aggregate = tracer.startTurn({ sessionId: 's', turnId: plan, usagePlan: plan });
    aggregate.child({ id: 'compact', kind: 'compaction', fidelity: 'aggregate-turn', usageContribution: 'exclusive' }).end({ usage });
    aggregate.end({ usage });
  }
  await tracer.shutdown();
  const all = spans(f), additive = all.filter(s => attrs(s)['fi.agent.usage.additive'] === true);
  assert.equal(additive.length, 3);
  assert.equal(all.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, 1);
  const summary = additive.find(s => attrs(s)['fi.agent.event'] === 'compaction');
  assert.equal(attrs(summary)['fi.agent.fidelity'], 'aggregate-turn');
  assert.equal(attrs(summary)['fi.span.kind'], 'AGENT');
  assert.equal(additive.reduce((sum, s) => sum + Number(attrs(s)['gen_ai.usage.total_tokens']), 0), 360);
  assert.equal(additive.reduce((sum, s) => sum + Number(attrs(s)['gen_ai.cost.total']), 0), 0.03);
});

async function heldWriter(t, options, path, operation) {
  const { spawn } = require('node:child_process');
  // Pause the actual protected writer immediately before rename, after it owns
  // the maintained lock. Its heartbeat remains live until the process is killed.
  const source = `const fs = require('node:fs/promises');
    const c = require(${JSON.stringify(require.resolve('../../../dist/src/index.js'))});
    const rename = fs.rename;
    fs.rename = async function(from, to) {
      if (to === ${JSON.stringify(path)}) {
        process.send('held');
        await new Promise(r => process.once('message', r));
      }
      return rename.call(this, from, to);
    };
    (${operation})(${JSON.stringify(options)}).then(() => process.exit(0), () => process.exit(2));
    setInterval(() => {}, 1000);`;
  const child = spawn(process.execPath, ['-e', source], { stdio: ['ignore', 'ignore', 'inherit', 'ipc'] });
  const exited = new Promise(r => child.once('exit', r));
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); await exited; });
  await new Promise((resolve, reject) => {
    child.once('message', resolve); child.once('error', reject);
    child.once('exit', code => reject(new Error(`writer exited early: ${code}`)));
  });
  return { child, exited };
}

test('killed writer lock recovers and disable fences an already-running exporter', { timeout: 20000 }, async t => {
  const f = await fixture(t), tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: 's', turnId: 'active-before-disable' });
  const held = await heldWriter(t, f.options, f.config.paths.manifest, 'c.disable');
  assert.equal(c.collectionEnabled(f.config), true);
  held.child.kill('SIGKILL'); await held.exited;
  await c.disable(f.options);
  assert.equal(c.collectionEnabled(f.config), false);
  turn.end({ output: 'must not export after disable' });
  await tracer.shutdown();
  assert.equal(spans(f).length, 0);
});

test('overlapping writers wait for live ownership and retry a fresh merge', { timeout: 20000 }, async t => {
  const f = await fixture(t), file = path.join(f.home, 'overlap.json');
  await c.updateJson(file, () => ({ count: 0 }));
  const held = await heldWriter(t, { file }, file, '(options) => c.updateJson(options.file, old => ({ count: old.count + 1 }))');
  let saved = false;
  const second = c.updateJson(file, old => ({ count: old.count + 1 })).then(() => { saved = true; });
  // Wait beyond the stale threshold: a live writer's heartbeat must retain ownership.
  await new Promise(r => setTimeout(r, 6100));
  assert.equal(saved, false);
  assert.equal((await c.readJson(file)).count, 0);
  held.child.send('release'); assert.equal(await held.exited, 0);
  await second;
  assert.equal((await c.readJson(file)).count, 2);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
});

test('protected writer refuses symlink and writable lock directories', async t => {
  const f = await fixture(t), file = path.join(f.home, 'protected.json'), lock = file + '.write-lock';
  const target = path.join(f.home, 'target'); await fs.mkdir(target);
  await fs.symlink(target, lock);
  await assert.rejects(c.writeProtected(file, '{}'), /UNSAFE_LOCK/);
  await fs.unlink(lock); await fs.mkdir(lock); await fs.chmod(lock, 0o777);
  await assert.rejects(c.writeProtected(file, '{}'), /UNSAFE_LOCK/);
  await fs.chmod(lock, 0o700);
});
