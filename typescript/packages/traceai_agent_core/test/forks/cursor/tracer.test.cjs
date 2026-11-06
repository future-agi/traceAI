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
for (const mode of ["revoked", "unreadable"])
  test(`delivery policy ${mode} after spans are queued fails closed`, async (t) => {
    const f = await fixture(t);
    let checks = 0;
    const tracer = c.createTurnTracer({
      config: f.config,
      beforeExport: async () => {
        checks++;
        const latest = await c.resolveConfig(f.options);
        return latest.captureContent;
      },
    });
    t.after(() => tracer.discard());
    tracer.startTurn({ sessionId: "privacy", turnId: mode, input: "PRIVATE" }).end();
    await c.writeProtected(
      f.config.paths.user,
      mode === "unreadable"
        ? "invalid-json"
        : JSON.stringify({ schemaVersion: 1, captureContent: false }),
    );
    await tracer.flush();
    await tracer.shutdown();
    assert.equal(checks, 1);
    assert.equal(spans(f).length, 0);
    assert.equal(tracer.state.pending, 0);
    assert.equal(tracer.state.dropped, 1);
    assert.equal(tracer.state.lastError, "EXPORT_POLICY_REVOKED");
  });
test("delivery policy is rechecked before each 413 split and revocation is sticky", async (t) => {
  let allowed = true;
  const f = await fixture(t, (_body, res) => {
    allowed = false;
    res.statusCode = 413;
    res.end("{}");
  });
  let checks = 0;
  const tracer = c.createTurnTracer({
    config: f.config,
    beforeExport: async () => {
      checks++;
      return allowed;
    },
  });
  t.after(() => tracer.discard());
  const turn = tracer.startTurn({ sessionId: "privacy", turnId: "split" });
  turn.child({ id: "tool", kind: "tool", input: "PRIVATE" }).end();
  turn.end();
  await tracer.flush();
  allowed = true;
  tracer.startTurn({ sessionId: "privacy", turnId: "after-revoke" }).end();
  await tracer.shutdown();
  assert.ok(checks >= 2);
  assert.equal(f.requests.filter((r) => r.body.resourceSpans.length).length, 1);
  assert.equal(tracer.state.pending, 0);
  assert.equal(tracer.state.exported, 0);
  assert.ok(tracer.state.dropped >= 2);
});
const usage = {
  input: 100,
  output: 20,
  cacheRead: 40,
  reasoning: 8,
  cacheInInput: true,
  reasoningInOutput: true,
  costUsd: 0.01,
};
test("all adapters receive structured credential redaction at the shared OTLP boundary", async (t) => {
  const f = await fixture(t);
  const tracer = c.createTurnTracer({
    config: {
      ...f.config,
      credentials: f.config.credentials,
      identityKey: f.config.identityKey,
      captureContent: true,
    },
  });
  const content = JSON.stringify({
    visible: "public",
    nested: [{ aws_secret_access_key: "SYNTHETIC_SHARED_CREDENTIAL" }],
    encoded: JSON.stringify({
      clientSecretKey: "SYNTHETIC_ENCODED_CREDENTIAL",
    }),
  });
  const turn = tracer.startTurn({
    sessionId: "privacy",
    turnId: "structured",
    input: content,
  });
  turn
    .child({ id: "model", kind: "generation", input: content })
    .end({ output: content });
  turn.end({ output: content });
  await tracer.shutdown();
  const all = spans(f);
  assert.equal(all.length, 2);
  for (const span of all)
    for (const field of ["input.value", "output.value"]) {
      assert.match(attrs(span)[field], /public/);
      assert.match(attrs(span)[field], /\[REDACTED\]/);
    }
  assert.ok(!JSON.stringify(all).includes("SYNTHETIC_SHARED_CREDENTIAL"));
  assert.ok(!JSON.stringify(all).includes("SYNTHETIC_ENCODED_CREDENTIAL"));
});
for (const usagePlan of ["request", "turn"])
  for (const size of [1000, 21000, 1048576, 1048577])
    test(`structured input and output are sanitized before display truncation: ${usagePlan}, ${size} bytes`, async (t) => {
      const f = await fixture(t);
      const tracer = c.createTurnTracer({ config: f.config });
      const marker = "SYNTHETIC_LARGE_CONTENT_CREDENTIAL";
      const payload = {
        clientSecretKey: marker,
        visible: "PUBLIC_CONTENT",
        padding: "",
      };
      payload.padding = "x".repeat(size - JSON.stringify(payload).length);
      const content = JSON.stringify(payload);
      assert.equal(Buffer.byteLength(content), size);
      const turn = tracer.startTurn({
        sessionId: "large-content",
        turnId: `${usagePlan}-${size}`,
        usagePlan,
        input: content,
      });
      turn
        .child({
          id: "generation",
          kind: "generation",
          fidelity: "reconstructed-message",
          input: content,
        })
        .end({ output: content });
      turn.end({ output: content });
      await tracer.shutdown();
      const all = spans(f);
      assert.equal(all.length, 2);
      assert.equal(
        all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
        1,
      );
      assert.ok(!JSON.stringify(all).includes(marker));
      for (const span of all)
        for (const field of ["input.value", "output.value"]) {
          const text = attrs(span)[field];
          if (size > 1048576) assert.equal(text, "[REDACTED_SIZE_LIMIT]");
          else {
            assert.match(text, /PUBLIC_CONTENT/);
            assert.match(text, /\[REDACTED\]/);
            assert.ok(text.length <= 20000);
            if (size > 20000) assert.equal(text.length, 20000);
          }
        }
    });
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

test("source-message generations render as LLM with explicitly reconstructed timing and one additive usage owner", async (t) => {
  const f = await fixture(t),
    tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({
    sessionId: "cursor-session",
    turnId: "cursor-turn",
    usagePlan: "request",
  });
  turn
    .child({
      id: "first-message",
      kind: "generation",
      fidelity: "reconstructed-message",
      timing: "reconstructed",
      model: "grok-4.7",
    })
    .end({ output: "first" });
  turn
    .child({
      id: "last-message",
      kind: "generation",
      fidelity: "reconstructed-message",
      timing: "reconstructed",
      model: "grok-4.7",
    })
    .end({ output: "last", usage });
  turn
    .child({
      id: "not-a-message",
      kind: "llm",
      fidelity: "reconstructed-message",
    })
    .end();
  turn.end();
  await tracer.shutdown();
  const all = spans(f),
    model = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(model.length, 2);
  assert.ok(
    model.every((s) => attrs(s)["fi.agent.timing"] === "reconstructed"),
  );
  assert.equal(
    all.filter((s) => attrs(s)["gen_ai.usage.input_tokens"] !== undefined)
      .length,
    1,
  );
  assert.equal(Number(attrs(model[1])["gen_ai.usage.input_tokens"]), 100);
  assert.equal(
    attrs(all.find((s) => !s.parentSpanId))["gen_ai.usage.input_tokens"],
    undefined,
  );
});

test("turn usage belongs to the last source generation, or the root if no generation exists", async (t) => {
  const f = await fixture(t),
    tracer = c.createTurnTracer({ config: f.config });
  for (const withModels of [true, false]) {
    const turn = tracer.startTurn({
      sessionId: "round2",
      turnId: String(withModels),
      usagePlan: "turn",
    });
    if (withModels) {
      const first = turn.child({
        id: "first",
        name: "first",
        kind: "generation",
        fidelity: "reconstructed-message",
      });
      const last = turn.child({
        id: "last",
        name: "last",
        kind: "generation",
        fidelity: "reconstructed-message",
      });
      last.end({ output: "final response" });
      first.end();
    } else turn.child({ id: "tool", kind: "tool" }).end();
    turn.end({ usage, gap: withModels ? undefined : "TRANSCRIPT_UNAVAILABLE" });
    turn.end({ usage });
  }
  await tracer.shutdown();
  const all = spans(f),
    bearers = all.filter(
      (s) => attrs(s)["gen_ai.usage.total_tokens"] !== undefined,
    );
  assert.equal(bearers.length, 2);
  assert.equal(
    bearers.reduce(
      (n, s) => n + Number(attrs(s)["gen_ai.usage.total_tokens"]),
      0,
    ),
    240,
  );
  const model = bearers.find((s) => s.parentSpanId),
    root = bearers.find((s) => !s.parentSpanId);
  assert.equal(model.name, "last");
  assert.equal(attrs(model)["output.value"], "final response");
  assert.equal(attrs(root)["fi.agent.gap"], "TRANSCRIPT_UNAVAILABLE");
  assert.ok(
    bearers.every((s) => attrs(s)["fi.usage.contribution"] === "additive"),
  );
});
