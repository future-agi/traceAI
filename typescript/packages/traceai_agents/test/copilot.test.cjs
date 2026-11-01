const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { minimize } = require("@traceai/agent-core");
const {
  COPILOT_SCHEMA,
  normalizeCopilotCli,
  copilotCliAdapter,
  copilotVSCodeAdapter,
} = require("../dist/src/profiles/copilot.js");
const { enforceCapability } = require("../dist/src/adapters.js");
const {
  encodeFacts,
  decodeTraces,
  encodeTraceObject,
} = require("../dist/src/export/otlp.js");
const { startReceiver } = require("../dist/src/runtime/receiver.js");
const { Spool } = require("../dist/src/storage/store.js");
const fixture = require("../../../../integrations/coding-agents/fixtures/source/copilot-cli-1.0.98.json");
const vscode = require("../../../../integrations/coding-agents/fixtures/source/copilot-vscode-documented-shapes.json");
const context = {
  identityKey: Buffer.alloc(32, 7),
  policyRevision: "1",
  sourceSchema: COPILOT_SCHEMA,
};
const binding = {
  id: "00000000-0000-4000-8000-000000000001",
  endpoint: "http://127.0.0.1:1/v1/traces",
  approvedOrigin: "http://127.0.0.1:1",
  project: "fixture",
  loopbackTest: true,
  backendContract: "immutable-agent-v1",
};
const base = path.resolve(__dirname, "../../../../../copilot-tests");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
function temp(t) {
  const dir = fs.mkdtempSync(path.join(base, "test-"));
  t.after(() => fs.rmSync(dir, { force: true, recursive: true }));
  return dir;
}
function envelopes(data = structuredClone(fixture)) {
  return data.resourceSpans.flatMap((r) =>
    r.scopeSpans.flatMap((s) =>
      s.spans.map((span) => ({ span, resource: r.resource, scope: s.scope })),
    ),
  );
}
function normalized(data) {
  return envelopes(data).map((e) => normalizeCopilotCli(e, context));
}
function protobufFixture(data) {
  const wire = structuredClone(data);
  for (const { span } of envelopes(wire)) {
    for (const key of ["traceId", "spanId", "parentSpanId"])
      if (span[key]) span[key] = Buffer.from(span[key], "hex");
    for (const link of span.links ?? []) {
      link.traceId = Buffer.from(link.traceId, "hex");
      link.spanId = Buffer.from(link.spanId, "hex");
    }
  }
  return encodeTraceObject(wire);
}
function attr(span, key) {
  return span.attributes.find((a) => a.key === key)?.value;
}
function put(span, key, value) {
  span.attributes = span.attributes.filter((a) => a.key !== key);
  span.attributes.push({ key, value });
}
function totals(facts) {
  const requests = facts.filter((f) => f.usage?.contribution === "request");
  return [
    requests.reduce((n, f) => n + (f.usage.input ?? 0), 0),
    requests.reduce((n, f) => n + (f.usage.output ?? 0), 0),
    requests.length,
  ];
}
test("pinned native 42925/146 fixture: root summaries never contribute or become USD", () => {
  const facts = normalized();
  assert.deepEqual(totals(facts), [42925, 146, 3]);
  for (const f of facts) enforceCapability(f, copilotCliAdapter.capability);
  const root = facts.find((f) => !f.parentSpanId);
  assert.equal(root.unit, "turn");
  assert.equal(root.usage.contribution, "none");
  assert.deepEqual(root.native.summary, { input: 42925, output: 146 });
  assert.equal(root.native.billing.multiplier, 3);
  assert.equal(root.usage.input, undefined);
  assert.equal(root.usage.costUsd, undefined);
  assert.equal(new Set(facts.map((f) => f.sessionId)).size, 1);
  assert.equal(new Set(facts.map((f) => f.usage.planId)).size, 1);
  const wire = decodeTraces(encodeFacts(facts, binding, "0.1.0"));
  const spans = wire.resourceSpans.flatMap((r) =>
    r.scopeSpans.flatMap((s) => s.spans),
  );
  const outputRoot = spans.find((s) => !s.parentSpanId);
  for (const k of [
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.cost.total",
    "gen_ai.usage.total_cost",
  ])
    assert.equal(attr(outputRoot, k), undefined);
  assert.equal(
    attr(outputRoot, "fi.source.summary.input_tokens").doubleValue,
    42925,
  );
  assert.equal(attr(outputRoot, "fi.source.billing.multiplier").doubleValue, 3);
  assert.equal(attr(outputRoot, "fi.usage.contribution").stringValue, "none");
  assert.equal(spans.filter((s) => s.kind === 3).length, 3);
  for (let i = 0; i < facts.length; i++) {
    assert.equal(
      Buffer.from(spans[i].traceId, "base64").toString("hex"),
      facts[i].traceId,
    );
    assert.equal(
      Buffer.from(spans[i].spanId, "base64").toString("hex"),
      facts[i].spanId,
    );
    assert.equal(spans[i].startTimeUnixNano, facts[i].startNs);
    assert.equal(spans[i].endTimeUnixNano, facts[i].endNs);
    assert.equal(
      spans[i].parentSpanId
        ? Buffer.from(spans[i].parentSpanId, "base64").toString("hex")
        : undefined,
      facts[i].parentSpanId,
    );
  }
});
test("root-only and nested summaries stay non-additive regardless of batching", () => {
  const source = envelopes();
  const root = source.find((e) => !e.span.parentSpanId);
  assert.equal(normalizeCopilotCli(root, context).usage.contribution, "none");
  const child = structuredClone(root);
  child.span.spanId = "9999999999999991";
  child.span.parentSpanId = root.span.spanId;
  const n = normalizeCopilotCli(child, context);
  assert.equal(n.parentSpanId, root.span.spanId);
  assert.equal(n.usage.contribution, "none");
  assert.notEqual(
    n.sourceWorkId,
    normalizeCopilotCli(root, context).sourceWorkId,
  );
  const request = source.find((e) => e.span.kind === 3);
  request.span.parentSpanId = child.span.spanId;
  assert.equal(
    normalizeCopilotCli(request, context).parentSpanId,
    child.span.spanId,
  );
});
test("session grouping spans turns, and native cross-trace links survive without raw attributes", () => {
  const e = envelopes().find((e) => e.span.kind === 3);
  const first = normalizeCopilotCli(e, context);
  e.span.traceId = "88888888888888888888888888888888";
  e.span.links = [
    {
      traceId: first.traceId,
      spanId: first.spanId,
      attributes: [
        { key: "private", value: { stringValue: "CANARY_PRIVATE_LINK" } },
      ],
    },
  ];
  const next = normalizeCopilotCli(e, context);
  assert.equal(first.sessionId, next.sessionId);
  assert.notEqual(first.turnId, next.turnId);
  assert.notEqual(first.usage.planId, next.usage.planId);
  assert.deepEqual(next.links, [
    { traceId: first.traceId, spanId: first.spanId },
  ]);
  assert.ok(!JSON.stringify(next).includes("CANARY_PRIVATE"));
  e.span.links = Array(33).fill(e.span.links[0]);
  assert.throws(() => normalizeCopilotCli(e, context), /INVALID_NATIVE_LINKS/);
});
test("equal-value distinct calls count twice; telemetry replay and policy revision retain work identity", () => {
  const first = envelopes().find((e) => e.span.kind === 3);
  const equal = structuredClone(first);
  equal.span.spanId = "9999999999999992";
  const a = normalizeCopilotCli(first, context),
    b = normalizeCopilotCli(equal, context);
  assert.deepEqual(totals([a, b]), [28190, 122, 2]);
  assert.notEqual(a.sourceWorkId, b.sourceWorkId);
  assert.notDeepEqual(a.usage.factIds, b.usage.factIds);
  const replay = normalizeCopilotCli(first, {
    ...context,
    policyRevision: "2",
  });
  assert.equal(replay.sourceWorkId, a.sourceWorkId);
  assert.deepEqual(replay.usage, a.usage);
  assert.notEqual(
    normalizeCopilotCli(first, { ...context, identityKey: Buffer.alloc(32, 8) })
      .sessionId,
    a.sessionId,
  );
});
test("cache/reasoning remain included once, conflicting aliases and invalid usage hold", () => {
  const request = envelopes().find((e) => e.span.kind === 3);
  put(request.span, "gen_ai.usage.reasoning.output_tokens", { intValue: "20" });
  put(request.span, "gen_ai.usage.cache_creation.input_tokens", {
    intValue: "14092",
  });
  const f = normalizeCopilotCli(request, context);
  assert.equal(f.usage.input, 14095);
  assert.equal(f.usage.output, 61);
  assert.equal(f.usage.reasoning, 20);
  assert.equal(f.usage.cacheWrite, 14092);
  put(request.span, "gen_ai.usage.cache_creation.input_tokens", {
    intValue: "1",
  });
  assert.throws(
    () => normalizeCopilotCli(request, context),
    /CONFLICTING_USAGE_ALIASES/,
  );
  request.span.attributes = request.span.attributes.filter(
    (a) => a.key !== "gen_ai.usage.cache_creation.input_tokens",
  );
  for (const value of [
    { intValue: "-1" },
    { intValue: "9007199254740992" },
    { doubleValue: 1.5 },
    { intValue: "" },
    { boolValue: false },
    { doubleValue: NaN },
  ]) {
    put(request.span, "gen_ai.usage.input_tokens", value);
    assert.throws(
      () => normalizeCopilotCli(request, context),
      /UNRESOLVED_USAGE/,
    );
  }
});
test("unknown usage/model stay absent, no inferred provider or fake zero cost", () => {
  const e = envelopes().find((e) => e.span.kind === 3);
  e.span.attributes = e.span.attributes.filter(
    (a) =>
      !a.key.startsWith("gen_ai.usage.") && a.key !== "gen_ai.provider.name",
  );
  put(e.span, "gen_ai.request.model", { stringValue: "private-model-canary" });
  const f = normalizeCopilotCli(e, context);
  assert.equal(f.usage.contribution, "request");
  assert.equal(f.usage.complete, false);
  assert.equal(f.usage.input, undefined);
  assert.equal(f.usage.costUsd, undefined);
  assert.equal(f.provider, undefined);
  assert.equal(f.model, undefined);
});
test("privacy before persistence: free text, source identities, dynamic tool labels and extra metadata disappear", () => {
  const e = envelopes()[0];
  e.span.name = "CANARY_PRIVATE_NAME";
  e.span.status = { code: 2, message: "CANARY_PRIVATE_ERROR" };
  e.span.events = [
    {
      name: "CANARY_PRIVATE_EVENT",
      attributes: [
        {
          key: "exception.message",
          value: { stringValue: "CANARY_PRIVATE_STACK" },
        },
      ],
    },
  ];
  for (const key of [
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.definitions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "enduser.pseudo.id",
    "gen_ai.tool.name",
    "gen_ai.tool.call.id",
    "gen_ai.response.id",
    "github.copilot.context.mcp_server_names",
    "gen_ai.conversation.id",
  ])
    put(e.span, key, { stringValue: "CANARY_PRIVATE_SOURCE" });
  e.resource.attributes.push({
    key: "session.id",
    value: { stringValue: "CANARY_PRIVATE_WINDOW" },
  });
  const f = normalizeCopilotCli(e, context);
  assert.equal(f.outcome, "error");
  assert.equal(f.native.statusCode, 2);
  f.native.summary = { input: 1, arbitrary: "CANARY_PRIVATE_SUMMARY" };
  f.native.billing = { multiplier: 1, arbitrary: "CANARY_PRIVATE_BILLING" };
  f.native.arbitrary = "CANARY_PRIVATE_META";
  const clean = minimize(f);
  assert.ok(!JSON.stringify(clean).includes("CANARY_PRIVATE"));
  assert.ok(
    !JSON.stringify(
      decodeTraces(encodeFacts([clean], binding, "0.1.0")),
    ).includes("CANARY_PRIVATE"),
  );
  clean.native.summary.input = -1;
  assert.throws(() => minimize(clean), /INVALID_FACT/);
});
test("version, scope, service, schema, kind, operation and conversation identity fail closed", () => {
  for (const mutate of [
    (e) => (e.scope.version = "2.0.0"),
    (e) => (e.scope.name = "other-agent"),
    (e) =>
      (e.resource.attributes.find(
        (a) => a.key === "service.name",
      ).value.stringValue = "other-agent"),
    (e) =>
      (e.resource.attributes.find(
        (a) => a.key === "service.version",
      ).value.stringValue = "1.0.99"),
    (e) => (e.span.kind = 3),
    (e) => (e.span.traceId = "0".repeat(32)),
    (e) => (e.span.endTimeUnixNano = "0"),
    (e) => put(e.span, "gen_ai.operation.name", { stringValue: "unknown" }),
    (e) =>
      (e.span.attributes = e.span.attributes.filter(
        (a) => a.key !== "gen_ai.conversation.id",
      )),
    (e) => e.span.attributes.push(e.span.attributes[0]),
  ]) {
    const e = envelopes()[0];
    mutate(e);
    assert.throws(() => normalizeCopilotCli(e, context));
  }
  assert.throws(
    () =>
      normalizeCopilotCli(envelopes()[0], {
        ...context,
        sourceSchema: "unknown",
      }),
    /UNKNOWN_SOURCE_SCHEMA/,
  );
});
test("execute_hook remains CHAIN activity without usage, preserving native error status", () => {
  const e = envelopes()[0];
  put(e.span, "gen_ai.operation.name", { stringValue: "execute_hook" });
  e.span.status = { code: 2 };
  const f = normalizeCopilotCli(e, context);
  assert.equal(f.unit, "activity");
  assert.equal(f.usage.contribution, "none");
  const s = decodeTraces(encodeFacts([f], binding, "0.1.0")).resourceSpans[0]
    .scopeSpans[0].spans[0];
  assert.equal(attr(s, "fi.span.kind").stringValue, "CHAIN");
  assert.equal(attr(s, "gen_ai.operation.name").stringValue, "execute_hook");
  assert.equal(s.status.code, 2);
});
for (const type of ["application/json", "application/x-protobuf"])
  test(
    "native receiver keeps producer qualification context for " + type,
    async (t) => {
      const received = [];
      let reject = false;
      const server = await startReceiver({
        coexistence: { state: "owned-or-none" },
        token: "synthetic-loopback-only",
        normalize: (_span, envelope) => normalizeCopilotCli(envelope, context),
        admit: async (f) => {
          if (reject)
            throw new (require("../dist/src/security/files.js").AgentError)(
              "HELD_IMMUTABLE_CONFLICT",
              3,
            );
          received.push(f);
        },
      });
      t.after(() => new Promise((r) => server.close(r)));
      const url = `http://127.0.0.1:${server.address().port}/v1/traces`;
      const send = (data) =>
        fetch(url, {
          method: "POST",
          headers: {
            "content-type": type,
            authorization: "Bearer synthetic-loopback-only",
          },
          body:
            type === "application/json"
              ? JSON.stringify(data)
              : protobufFixture(data),
        });
      const ack = await send(fixture);
      assert.equal(ack.status, 200);
      assert.equal(ack.headers.get("content-type"), type);
      if (type === "application/json") assert.deepEqual(await ack.json(), {});
      else
        assert.equal(
          require("../dist/src/export/otlp.js").decodeRejections(
            Buffer.from(await ack.arrayBuffer()),
          ),
          0,
        );
      assert.deepEqual(totals(received), [42925, 146, 3]);
      assert.deepEqual(received, normalized());
      reject = true;
      const partial = await send(fixture);
      assert.equal(partial.headers.get("content-type"), type);
      if (type === "application/json")
        assert.deepEqual(await partial.json(), {
          partialSuccess: { rejectedSpans: "7" },
        });
      else
        assert.equal(
          require("../dist/src/export/otlp.js").decodeRejections(
            Buffer.from(await partial.arrayBuffer()),
          ),
          7,
        );
      const bad = structuredClone(fixture);
      bad.resourceSpans[0].scopeSpans[0].scope.version = "2.0.0";
      assert.equal((await send(bad)).status, 400);
      assert.equal(received.length, 7);
    },
  );
test("native codec is explicit: protobuf byte lengths cannot masquerade as JSON hex IDs", () => {
  const encoded = decodeTraces(protobufFixture(fixture));
  assert.deepEqual(
    envelopes(encoded).map((e) =>
      normalizeCopilotCli({ ...e, idEncoding: "base64" }, context),
    ),
    normalized(),
  );
  // protobufjs treats hex strings as base64, producing invalid 24/12-byte IDs.
  const malformed = decodeTraces(encodeTraceObject(fixture));
  for (const e of envelopes(malformed))
    assert.throws(
      () => normalizeCopilotCli({ ...e, idEncoding: "base64" }, context),
      /INVALID_NATIVE_ID/,
    );
  for (const e of envelopes(encoded))
    assert.throws(() => normalizeCopilotCli(e, context), /INVALID_NATIVE_ID/);
  for (const field of ["traceId", "spanId", "parentSpanId"]) {
    const e = envelopes(encoded)[0];
    e.span[field] = envelopes()[0].span[field];
    assert.throws(
      () => normalizeCopilotCli({ ...e, idEncoding: "base64" }, context),
      /INVALID_NATIVE_ID/,
    );
  }
  const e = envelopes(encoded)[0];
  e.span.links = [{ traceId: e.span.traceId, spanId: "0000000000000001" }];
  assert.throws(
    () => normalizeCopilotCli({ ...e, idEncoding: "base64" }, context),
    /INVALID_NATIVE_ID/,
  );
});
test("native JSON/protobuf replays share durable work identity; malformed bytes are not admitted", async (t) => {
  const dir = fs.mkdtempSync(path.join(base, "test-"));
  const spool = new Spool(path.join(dir, "spool"), "0.1.0");
  let server;
  t.after(async () => {
    if (server) await new Promise((r) => server.close(r));
    spool.close();
    fs.rmSync(dir, { force: true, recursive: true });
  });
  const route = {
    id: "copilot.cli",
    binding,
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  };
  spool.route(route);
  const admitted = [];
  server = await startReceiver({
    coexistence: { state: "owned-or-none" },
    singleUserApproved: true,
    normalize: (_span, envelope) => normalizeCopilotCli(envelope, context),
    admit: async (f) => admitted.push(spool.admit(route.id, f)),
  });
  const send = (type, body) =>
    fetch(`http://127.0.0.1:${server.address().port}/v1/traces`, {
      method: "POST",
      headers: { "content-type": type },
      body,
    });
  for (const [type, body] of [
    ["application/json", JSON.stringify(fixture)],
    ["application/x-protobuf", protobufFixture(fixture)],
  ]) {
    const ack = await send(type, body);
    assert.equal(ack.status, 200);
    await ack.arrayBuffer();
  }
  assert.deepEqual(
    admitted.map((a) => a.duplicate),
    [...Array(7).fill(false), ...Array(7).fill(true)],
  );
  const bad = await send("application/x-protobuf", encodeTraceObject(fixture));
  assert.equal(bad.status, 400);
  await bad.arrayBuffer();
  assert.equal(admitted.length, 14);
  let input = 0,
    output = 0,
    calls = 0;
  for (;;) {
    const row = spool.next(route.id);
    if (!row) break;
    for (const { span } of envelopes(decodeTraces(row.bytes))) {
      input += attr(span, "gen_ai.usage.input_tokens")?.doubleValue ?? 0;
      output += attr(span, "gen_ai.usage.output_tokens")?.doubleValue ?? 0;
      if (attr(span, "fi.usage.contribution")?.stringValue === "request")
        calls++;
    }
    spool.complete(row.id, row.epoch, {
      state: "accepted",
      code: "HTTP_ACCEPTED",
    });
  }
  assert.deepEqual([input, output, calls], [42925, 146, 3]);
});
test("durable spool replay preserves exact usage and discards raw private data before disk", (t) => {
  const dir = temp(t),
    spool = new Spool(path.join(dir, "spool"), "0.1.0");
  const route = {
    id: "copilot.cli",
    binding,
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  };
  spool.route(route);
  try {
    const dirty = structuredClone(fixture);
    for (const e of envelopes(dirty)) {
      e.span.name = "CANARY_PRIVATE_DISK";
      put(e.span, "gen_ai.input.messages", {
        stringValue: "CANARY_PRIVATE_DISK",
      });
      put(e.span, "gen_ai.system_instructions", {
        stringValue: "CANARY_PRIVATE_DISK",
      });
    }
    const facts = normalized(dirty);
    // Native exporters may send root first, last or separately.
    for (const f of [...facts].reverse())
      assert.equal(spool.admit(route.id, f).duplicate, false);
    for (const f of facts)
      assert.equal(spool.admit(route.id, f).duplicate, true);
    for (const file of fs.readdirSync(path.join(dir, "spool")))
      assert.ok(
        !fs
          .readFileSync(path.join(dir, "spool", file))
          .includes(Buffer.from("CANARY_PRIVATE_DISK")),
      );
    let input = 0,
      output = 0,
      calls = 0;
    for (;;) {
      const row = spool.next(route.id);
      if (!row) break;
      const spans = decodeTraces(row.bytes).resourceSpans.flatMap((r) =>
        r.scopeSpans.flatMap((s) => s.spans),
      );
      for (const s of spans) {
        input += attr(s, "gen_ai.usage.input_tokens")?.doubleValue ?? 0;
        output += attr(s, "gen_ai.usage.output_tokens")?.doubleValue ?? 0;
        if (attr(s, "fi.usage.contribution")?.stringValue === "request")
          calls++;
      }
      spool.complete(row.id, row.epoch, {
        state: "accepted",
        code: "HTTP_ACCEPTED",
      });
    }
    assert.deepEqual([input, output, calls], [42925, 146, 3]);
    for (const f of facts)
      assert.equal(spool.admit(route.id, f).duplicate, true);
  } finally {
    spool.close();
  }
});
test("VS Code documented local/wrapper/terminal shapes are distinct and not falsely qualified", () => {
  assert.equal(vscode.qualified, false);
  for (const harness of vscode.harnesses)
    assert.throws(
      () => copilotVSCodeAdapter.normalize(harness, context),
      /COPILOT_VSCODE_HARNESS_UNQUALIFIED/,
    );
  const wrapper = vscode.harnesses.find((h) => h.surface === "copilot");
  assert.equal(wrapper.spans[1].parentSpanId, wrapper.spans[0].spanId);
  const terminal = vscode.harnesses.find((h) => h.surface === "terminal");
  assert.notEqual(terminal.spans[0].traceId, wrapper.spans[0].traceId);
  assert.equal(terminal.spans[0].parentSpanId, undefined);
});
test("CLI doctor and enable refuse safely without auth; disable/uninstall are idempotent and leave config alone", (t) => {
  const dir = temp(t);
  const env = {
    PATH: process.env.PATH,
    HOME: dir,
    FI_AGENT_CONFIG_HOME: path.join(dir, "config"),
    FI_AGENT_STATE_HOME: path.join(dir, "state"),
    OTEL_EXPORTER_OTLP_ENDPOINT:
      "https://unowned.invalid/?CANARY_PRIVATE_ENDPOINT",
    OTEL_EXPORTER_OTLP_HEADERS: "authorization=CANARY_PRIVATE_HEADER",
  };
  for (const surface of ["cli", "vscode"])
    for (const command of [
      "doctor",
      "enable",
      "run",
      "update",
      "disable",
      "uninstall",
    ]) {
      const r = spawnSync(
        process.execPath,
        [
          path.resolve(__dirname, "../bin/fi-agent.cjs"),
          command,
          "copilot",
          "--surface",
          surface,
        ],
        { env, encoding: "utf8", timeout: 10000 },
      );
      const removing = ["disable", "uninstall"].includes(command);
      assert.equal(r.status, removing ? 0 : 5, r.stdout + r.stderr);
      assert.equal(r.stderr, "");
      assert.ok(!r.stdout.includes("CANARY_PRIVATE"));
      const result = JSON.parse(r.stdout);
      if (!removing) {
        assert.equal(result.activation.activated, "unknown");
        assert.equal(result.persistence, "unknown");
      }
      assert.equal(fs.existsSync(path.join(dir, "config")), false);
      assert.equal(fs.existsSync(path.join(dir, "state")), false);
    }
});
