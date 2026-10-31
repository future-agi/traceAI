const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const {
  normalizeClaudeSpan,
  CLAUDE_SCHEMA,
} = require("../dist/src/adapters/claude-code/native.js");
const {
  claudeCodeAdapter,
  preflightClaude,
} = require("../dist/src/adapters/claude-code/index.js");
const { NativeIngress } = require("../dist/src/runtime/native.js");
const { Spool } = require("../dist/src/storage/store.js");
const {
  encodeTraceObject,
  decodeRejections,
  spanObject,
} = require("../dist/src/export/otlp.js");
const { agentEnvironment, runAgent } = require("../dist/src/cli/run.js");
const fixture = require("../contract/claude-fixture.cjs");
const context = {
  identityKey: Buffer.alloc(32, 1),
  policyRevision: "1",
  sourceSchema: CLAUDE_SCHEMA,
};
const normalize = (
  s = fixture.span(),
  r = fixture.resource,
  sc = fixture.scope,
  c = context,
) => normalizeClaudeSpan(s, r, sc, c);
const base = path.resolve(__dirname, "../../../../../native-tests");
fs.mkdirSync(base, { recursive: true, mode: 0o700 });
const temp = () => fs.mkdtempSync(path.join(base, "n-"));

test("packed capability profile matches the compiled adapter and keeps unqualified routes held", () => {
  const profile = require("../profiles/claude-code/native-v1.json");
  assert.deepEqual(profile.versions, claudeCodeAdapter.capability.versions);
  assert.deepEqual(profile.platforms, claudeCodeAdapter.capability.platforms);
  assert.equal(profile.source_schema, claudeCodeAdapter.capability.schemas[0]);
  assert.equal(
    profile.identity_profile,
    claudeCodeAdapter.capability.identityProfiles[0],
  );
  assert.equal(profile.capture, "structure");
  assert.equal(profile.transcript, "held-identity-join-unproven");
  assert.throws(
    () => claudeCodeAdapter.validateArgs([]),
    /CLAUDE_PRINT_SURFACE_REQUIRED/,
  );
  claudeCodeAdapter.validateArgs(["--print", "hello"]);
});
test("native IDs/parents/timing survive, identities are pseudonymized, content never crosses normalization", () => {
  const f = normalize();
  assert.equal(f.traceId, "a".repeat(32));
  assert.equal(f.spanId, "b".repeat(16));
  assert.equal(f.parentSpanId, "c".repeat(16));
  assert.equal(f.startNs, fixture.span().startTimeUnixNano);
  assert.equal(f.timing, "observed_request");
  assert.equal(f.unit, "request");
  assert.equal(f.outcome, "success");
  assert.equal(f.provider, undefined);
  assert.match(f.sessionId, /^[a-f0-9]{64}$/);
  assert.equal(f.inputCompleteness, "unavailable");
  assert.ok(!JSON.stringify(f).includes("PRIVACY_CANARY"));
  assert.ok(!JSON.stringify(spanObject(f)).includes("PRIVACY_CANARY"));
  assert.equal(f.usage.input, 20);
  assert.equal(f.usage.output, 2);
  assert.equal(f.usage.total, 22);
  assert.equal(f.usage.cacheRead, 7);
  assert.equal(f.usage.cacheWrite, 3);
  assert.equal(f.usage.costUsd, undefined);
  assert.deepEqual(normalize(), f);
  assert.equal(
    normalize(undefined, undefined, undefined, {
      ...context,
      policyRevision: "2",
    }).sourceWorkId,
    f.sourceWorkId,
  );
  assert.notEqual(
    normalize(undefined, undefined, undefined, {
      ...context,
      identityKey: Buffer.alloc(32, 2),
    }).sessionId,
    f.sessionId,
  );
});
test("unknown schema/operation/session or malformed counters fail closed; no fabricated zero cache or output", () => {
  assert.throws(
    () =>
      normalize(undefined, {
        attributes: fixture.kv({
          "service.name": "claude-code",
          "service.version": "2.1.317",
        }),
      }),
    /UNKNOWN_SOURCE_SCHEMA/,
  );
  assert.throws(
    () =>
      normalize(undefined, undefined, { ...fixture.scope, version: "2.0.0" }),
    /UNKNOWN_SOURCE_SCHEMA/,
  );
  assert.throws(
    () => normalize(fixture.span("claude_code.new_unknown")),
    /UNKNOWN_NATIVE_OPERATION/,
  );
  assert.throws(
    () => normalize(fixture.span(undefined, { traceId: "0".repeat(32) })),
    /INVALID_NATIVE_ID/,
  );
  const attrs = fixture.span().attributes;
  assert.throws(
    () =>
      normalize(
        fixture.span(undefined, {
          attributes: [...attrs, ...fixture.kv({ output_tokens: 2 })],
        }),
      ),
    /INVALID_NATIVE_ATTRIBUTES/,
  );
  assert.throws(
    () =>
      normalize(
        fixture.span(undefined, {
          attributes: attrs.filter((a) => a.key !== "session.id"),
        }),
      ),
    /HELD_NATIVE_SESSION/,
  );
  assert.throws(
    () =>
      normalize(
        fixture.span(undefined, {
          attributes: attrs.map((a) =>
            a.key === "output_tokens"
              ? fixture.kv({ output_tokens: -1 })[0]
              : a,
          ),
        }),
      ),
    /INVALID_NATIVE_USAGE/,
  );
  const missing = normalize(
    fixture.span(undefined, {
      attributes: attrs.filter(
        (a) => !["cache_creation_tokens", "output_tokens"].includes(a.key),
      ),
    }),
  );
  assert.equal(missing.usage.input, undefined);
  assert.equal(missing.usage.output, undefined);
  assert.equal(missing.usage.total, undefined);
  assert.equal(missing.usage.complete, false);
});
test("tools, execution, permission denial and parent summaries never become additional LLM usage", () => {
  const root = normalize(
    fixture.span("claude_code.interaction", {
      spanId: "c".repeat(16),
      parentSpanId: undefined,
    }),
  );
  const tool = normalize(
    fixture.span("claude_code.tool", { spanId: "d".repeat(16) }),
  );
  const execution = normalize(
    fixture.span("claude_code.tool.execution", {
      spanId: "e".repeat(16),
      parentSpanId: tool.spanId,
      status: { code: 2, message: "PRIVACY_CANARY" },
    }),
  );
  assert.equal(root.unit, "turn");
  assert.equal(root.usage, undefined);
  assert.equal(tool.unit, "tool");
  assert.equal(tool.tool, "Read");
  assert.equal(tool.usage, undefined);
  assert.equal(execution.unit, "activity");
  assert.equal(execution.outcome, "error");
  assert.equal(execution.usage, undefined);
  assert.equal(execution.parentSpanId, tool.spanId);
  for (const [decision, outcome] of [
    ["reject", "permission_denied"],
    ["cancelled", "cancelled"],
  ]) {
    const f = normalize(
      fixture.span("claude_code.tool.blocked_on_user", {
        attributes: [...fixture.span().attributes, ...fixture.kv({ decision })],
      }),
    );
    assert.equal(f.outcome, outcome);
  }
  const unknown = normalize(
    fixture.span("claude_code.tool", {
      attributes: fixture
        .span()
        .attributes.map((a) =>
          a.key === "tool_name_safe"
            ? fixture.kv({ tool_name_safe: "PRIVACY_CANARY" })[0]
            : a,
        ),
    }),
  );
  assert.equal(unknown.tool, "mcp_other");
});
test("native nested and detached child work preserves supplied parentage and links, never inferred ancestry", () => {
  const child = normalize(
    fixture.span(undefined, { parentSpanId: "d".repeat(16) }),
  );
  assert.equal(child.parentSpanId, "d".repeat(16));
  const detached = normalize(
    fixture.span(undefined, {
      traceId: "e".repeat(32),
      spanId: "f".repeat(16),
      parentSpanId: undefined,
      links: [
        {
          traceId: "a".repeat(32),
          spanId: "d".repeat(16),
          attributes: fixture.kv({ secret: "PRIVACY_CANARY" }),
        },
      ],
    }),
  );
  assert.equal(detached.parentSpanId, undefined);
  assert.equal(detached.traceId, "e".repeat(32));
  assert.deepEqual(detached.links, [
    { traceId: "a".repeat(32), spanId: "d".repeat(16) },
  ]);
  assert.equal(detached.sessionId, child.sessionId);
  assert.notEqual(detached.turnId, child.turnId);
  assert.ok(!JSON.stringify(detached).includes("PRIVACY_CANARY"));
});
test("native replay and two equal-value requests have immutable, distinct additive owners", () => {
  const dir = temp();
  const spool = new Spool(dir, "0.1.0");
  try {
    const binding = {
      id: "00000000-0000-4000-8000-000000000001",
      endpoint: "https://api.futureagi.com/tracer/v1/traces",
      approvedOrigin: "https://api.futureagi.com",
      project: "fixture",
      backendContract: "immutable-agent-v1",
    };
    spool.route({
      id: "claude-code.cli",
      binding,
      policy: { revision: "1", categories: [], ceiling: [] },
      enabled: true,
      epoch: 1,
    });
    const one = normalize(),
      two = normalize(fixture.span(undefined, { spanId: "d".repeat(16) }));
    assert.notEqual(one.sourceWorkId, two.sourceWorkId);
    assert.notDeepEqual(one.usage.factIds, two.usage.factIds);
    assert.equal(spool.admit("claude-code.cli", one).duplicate, false);
    assert.equal(spool.admit("claude-code.cli", one).duplicate, true);
    assert.equal(spool.admit("claude-code.cli", two).duplicate, false);
    assert.throws(
      () =>
        spool.admit("claude-code.cli", {
          ...one,
          usage: { ...one.usage, output: 3, total: 23 },
        }),
      /CONFLICT/,
    );
    assert.equal(one.usage.input + two.usage.input, 40);
  } finally {
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
function post(endpoint, token, data, protobuf = false, headers = {}) {
  return new Promise((resolve, reject) => {
    const body = protobuf
      ? encodeTraceObject(data)
      : Buffer.from(JSON.stringify(data));
    const req = http.request(
      endpoint,
      {
        method: "POST",
        headers: {
          authorization: "Bearer " + token,
          "content-type": protobuf
            ? "application/x-protobuf"
            : "application/json",
          ...headers,
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () =>
          resolve({ status: res.statusCode, body: Buffer.concat(chunks) }),
        );
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}
test("authenticated JSON/protobuf front door qualifies resources and shuts down on disable", async () => {
  const ingress = new NativeIngress();
  const got = [];
  try {
    const endpoint = await ingress.open(
      "claude-code.cli",
      claudeCodeAdapter,
      context,
      () => true,
      async (f) => got.push(f),
    );
    assert.equal(
      (await post(endpoint.endpoint, "wrong", fixture.envelope())).status,
      403,
    );
    assert.equal(
      (
        await post(
          endpoint.endpoint,
          endpoint.token,
          fixture.envelope(),
          false,
          { origin: "https://example.com" },
        )
      ).status,
      403,
    );
    for (const proto of [false, true]) {
      const r = await post(
        endpoint.endpoint,
        endpoint.token,
        fixture.envelope(),
        proto,
      );
      assert.equal(r.status, 200);
      if (proto) assert.equal(decodeRejections(r.body), 0);
      else assert.deepEqual(JSON.parse(r.body), {});
    }
    assert.equal(got.length, 2);
    assert.deepEqual(got[0], got[1]);
    assert.ok(ingress.activated.has("claude-code.cli"));
    const unknown = fixture.envelope();
    unknown.resourceSpans[0].resource = {
      attributes: fixture.kv({
        "service.name": "claude-code",
        "service.version": "0.0.0",
      }),
    };
    assert.equal(
      (await post(endpoint.endpoint, endpoint.token, unknown)).status,
      400,
    );
    assert.equal(got.length, 2);
    assert.equal(
      ingress.status("claude-code.cli").last_reason,
      "UNKNOWN_SOURCE_SCHEMA",
    );
    ingress.closeRoute("claude-code.cli");
    assert.equal(ingress.activated.size, 0);
    await assert.rejects(
      post(endpoint.endpoint, endpoint.token, fixture.envelope()),
    );
  } finally {
    ingress.stop();
  }
});
test("failed admission is rejected, not an activation receipt", async () => {
  const ingress = new NativeIngress();
  try {
    const e = await ingress.open(
      "claude-code.cli",
      claudeCodeAdapter,
      context,
      () => true,
      async () => {
        throw Error("DISK");
      },
    );
    const r = await post(e.endpoint, e.token, fixture.envelope());
    assert.equal(r.status, 503);
    assert.equal(
      ingress.status("claude-code.cli").last_reason,
      "NATIVE_ADMISSION_UNAVAILABLE",
    );
    assert.equal(ingress.activated.size, 0);
  } finally {
    ingress.stop();
  }
});
test("coexistence preflight refuses existing destinations, managed settings and settings overrides without changing files", async () => {
  const home = temp();
  const agent = path.join(home, "agent");
  fs.mkdirSync(agent);
  const env = { HOME: home, CLAUDE_CONFIG_DIR: agent };
  try {
    preflightClaude(env, home);
    for (const key of [
      "OTEL_EXPORTER_OTLP_ENDPOINT",
      "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
      "OTEL_EXPORTER_OTLP_HEADERS",
      "BETA_TRACING_ENDPOINT",
    ])
      assert.throws(
        () => preflightClaude({ ...env, [key]: "PRIVACY_CANARY" }, home),
        /NATIVE_DESTINATION_CONFLICT/,
      );
    assert.throws(
      () =>
        preflightClaude({ ...env, CLAUDE_CODE_ENABLE_TELEMETRY: "0" }, home),
      /NATIVE_POLICY_CONFLICT/,
    );
    const file = path.join(agent, "settings.json");
    const bytes = JSON.stringify({
      permissions: { allow: [] },
      env: { OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "PRIVACY_CANARY" },
    });
    fs.writeFileSync(file, bytes);
    assert.throws(
      () => preflightClaude(env, home),
      /NATIVE_DESTINATION_CONFLICT/,
    );
    assert.equal(fs.readFileSync(file, "utf8"), bytes);
    fs.unlinkSync(file);
    fs.writeFileSync(
      path.join(agent, "managed-settings.json"),
      "PRIVACY_CANARY",
    );
    assert.throws(
      () => preflightClaude(env, home),
      /MANAGED_TELEMETRY_UNQUALIFIED/,
    );
    assert.throws(
      () => claudeCodeAdapter.validateArgs(["--settings=x"]),
      /EFFECTIVE_CONFIG_OVERRIDE_UNQUALIFIED/,
    );
    await assert.rejects(
      claudeCodeAdapter.preflight("enable", { transcriptConsent: true }),
      /HELD_NATIVE_TRANSCRIPT_JOIN/,
    );
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});
test("transient native admission retries immutable batches without duplicate usage", async () => {
  const dir = temp();
  const spool = new Spool(dir, "0.1.0");
  const ingress = new NativeIngress();
  const route = "claude-code.cli";
  spool.route({
    id: route,
    binding: {
      id: "00000000-0000-4000-8000-000000000001",
      endpoint: "https://api.futureagi.com/tracer/v1/traces",
      approvedOrigin: "https://api.futureagi.com",
      project: "fixture",
      backendContract: "immutable-agent-v1",
    },
    policy: { revision: "1", categories: [], ceiling: [] },
    enabled: true,
    epoch: 1,
  });
  let unavailable = true;
  const admissions = [];
  try {
    const e = await ingress.open(
      route,
      claudeCodeAdapter,
      context,
      () => true,
      async (fact) => {
        if (fact.spanId === "d".repeat(16) && unavailable) {
          unavailable = false;
          throw new (require("../dist/src/security/files.js").AgentError)(
            "STORAGE_UNAVAILABLE",
            7,
          );
        }
        const result = spool.admit(route, fact);
        admissions.push(result.duplicate);
        return result;
      },
    );
    const batch = fixture.envelope();
    batch.resourceSpans[0].scopeSpans[0].spans.push(
      fixture.span(undefined, { spanId: "d".repeat(16) }),
    );
    assert.equal((await post(e.endpoint, e.token, batch, true)).status, 503);
    assert.deepEqual(admissions, [false]);
    assert.equal((await post(e.endpoint, e.token, batch, true)).status, 200);
    assert.deepEqual(admissions, [false, true, false]);
    assert.equal((await post(e.endpoint, e.token, batch, false)).status, 200);
    assert.deepEqual(admissions, [false, true, false, true, true]);
  } finally {
    ingress.stop();
    spool.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("Claude untraced fallback retains preflight and never opens native ingress", async () => {
  const home = temp();
  const previous = { ...process.env };
  const cwd = process.cwd();
  try {
    for (const key of Object.keys(process.env)) delete process.env[key];
    fs.writeFileSync(
      path.join(home, "claude"),
      '#!/bin/sh\nprintf "2.1.316 (Claude Code)\\n"\n',
      { mode: 0o700 },
    );
    Object.assign(process.env, {
      HOME: home,
      CLAUDE_CONFIG_DIR: path.join(home, "agent"),
      PATH: home,
    });
    process.chdir(home);
    const launch = await claudeCodeAdapter.launch(
      { socketPath: path.join(home, "missing.sock") },
      { tracing: false },
    );
    assert.equal(launch.executable, path.join(home, "claude"));
    assert.deepEqual(launch.env, {});
    assert.deepEqual(launch.args, []);
    assert.equal(launch.close, undefined);
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT =
      "https://example.invalid/v1/traces";
    await assert.rejects(
      claudeCodeAdapter.launch(
        { socketPath: path.join(home, "missing.sock") },
        { tracing: false },
      ),
      /NATIVE_DESTINATION_CONFLICT/,
    );
  } finally {
    process.chdir(cwd);
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, previous);
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("launch overlays preserve provider/permissions; failing telemetry close cannot change child status", async () => {
  const env = {
    ANTHROPIC_API_KEY: "fixture-provider",
    ANTHROPIC_BASE_URL: "http://127.0.0.1:1",
    FI_API_KEY: "fixture-fi",
    FI_AGENT_STATE_HOME: "fixture-state",
    OTEL_METRICS_EXPORTER: "otlp",
  };
  const actual = agentEnvironment(env, { CLAUDE_CODE_ENABLE_TELEMETRY: "1" });
  assert.equal(actual.ANTHROPIC_API_KEY, env.ANTHROPIC_API_KEY);
  assert.equal(actual.ANTHROPIC_BASE_URL, env.ANTHROPIC_BASE_URL);
  assert.equal(actual.FI_API_KEY, undefined);
  assert.equal(actual.OTEL_METRICS_EXPORTER, "otlp");
  assert.throws(
    () => agentEnvironment(env, { ANTHROPIC_BASE_URL: "changed" }),
    /UNSAFE_LAUNCH_OVERLAY/,
  );
  const result = await runAgent(
    {
      launch: async () => ({
        executable: process.execPath,
        env: {},
        args: ["-e", "process.exit(17)"],
        close: async () => {
          throw Error("OFFLINE");
        },
      }),
    },
    {},
    [],
  );
  assert.equal(result.code, 17);
});
