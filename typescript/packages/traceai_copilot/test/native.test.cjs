const { test } = require("node:test");
const assert = require("node:assert/strict");
const { SemanticConventions: SC } = require("@traceai/fi-semantic-conventions");
const fs = require("node:fs");
const path = require("node:path");
const {
  nativeSpan,
  NativeLines,
  NativeSpans,
  childEnvironment,
  copilotAdapter,
  supportedHostVersion,
  hostVersionFromOutput,
  runCopilot,
} = require("../dist/index.js");
const {
  cleanText,
  CONTENT_LIMIT,
  MAX_LINE_BYTES,
} = require("../dist/native.js");
const config = {
  captureContent: true,
  credentials: { apiKey: "test-fi-api", secretKey: "test-fi-secret" },
  userId: "test-user",
  projectName: "copilot-test",
  hideInputs: false,
  hideOutputs: false,
  hideErrorDetails: false,
};
const raw = () => ({
  type: "span",
  traceId: "a".repeat(32),
  spanId: "b".repeat(16),
  parentSpanId: "c".repeat(16),
  name: "unsafe private prompt",
  kind: 2,
  startTime: [1, 900000000],
  endTime: [2, 100000000],
  attributes: {
    "gen_ai.operation.name": "chat",
    "gen_ai.conversation.id": "conversation-1",
    "gen_ai.request.model": "gpt-4.1",
    "gen_ai.provider.name": "github",
    "gen_ai.usage.input_tokens": 30,
    "gen_ai.usage.output_tokens": 6,
    "gen_ai.usage.cache_read.input_tokens": 5,
    "gen_ai.usage.reasoning.output_tokens": 2,
    "gen_ai.input.messages": JSON.stringify([
      {
        role: "user",
        parts: [{ type: "text", content: "private-input test-fi-api" }],
      },
    ]),
    "gen_ai.output.messages": "private-output test-fi-secret",
    "gen_ai.system_instructions": "private-system",
    "unknown.field": "never-export",
    "server.address": "private.example",
  },
  status: { code: 2, message: "private-error test-fi-secret" },
  events: [
    {
      name: "exception",
      attributes: { "exception.message": "private-event test-fi-secret" },
    },
    { name: "arbitrary-content-event" },
  ],
  instrumentationScope: { name: "github.copilot", version: "1.0.106" },
});
test("native IDs, parent, session, times and request usage survive FI mapping", () => {
  const s = nativeSpan(raw(), config);
  assert.equal(s.spanContext().traceId, "a".repeat(32));
  assert.equal(s.parentSpanContext.spanId, "c".repeat(16));
  assert.equal(s.attributes["session.id"], "conversation-1");
  assert.equal(s.attributes["user.id"], "test-user");
  assert.equal(s.name, "chat gpt-4.1");
  assert.deepEqual(s.duration, [0, 200000000]);
  assert.equal(s.attributes[SC.FI_USAGE_CONTRIBUTION], "additive");
  assert.equal(s.attributes["gen_ai.usage.cache_read.input_tokens"], 5);
  assert.equal(s.resource.attributes.project_type, "observe");
  assert.equal(s.resource.attributes.project_name, "copilot-test");
  assert.ok(!JSON.stringify(s).includes("test-fi-"));
  assert.ok(!JSON.stringify(s).includes("never-export"));
});
test("structure-only drops content, status details, unknown fields and event attributes", () => {
  const s = nativeSpan(raw(), { ...config, captureContent: false });
  assert.ok(!JSON.stringify(s).includes("private-"));
  assert.equal(s.status.code, 2);
  assert.deepEqual(s.events[0].attributes, {});
});
test("input/output/error hide policies win over native content capture", () => {
  for (const [setting, missing, kept] of [
    ["hideInputs", "private-input", "private-output"],
    ["hideOutputs", "private-output", "private-input"],
  ]) {
    const s = nativeSpan(raw(), { ...config, [setting]: true });
    assert.ok(!JSON.stringify(s).includes(missing));
    assert.ok(JSON.stringify(s).includes(kept));
    assert.equal(s.status.message, undefined);
    assert.deepEqual(s.events[0].attributes, {});
  }
  const s = nativeSpan(raw(), { ...config, hideErrorDetails: true });
  assert.equal(s.status.message, undefined);
  assert.deepEqual(s.events[0].attributes, {});
});
test("known secret crossing the field cap is removed before truncation", () => {
  const prefix = "x".repeat(CONTENT_LIMIT - 4);
  const v = cleanText(prefix + "test-fi-secret", config);
  assert.equal(v.length, CONTENT_LIMIT);
  assert.ok(!v.includes("test"));
  assert.ok(v.endsWith("[RED"));
});
test("parent usage does not become duplicate FI request usage; nested agent keeps parent", () => {
  const r = raw();
  r.attributes["gen_ai.operation.name"] = "invoke_agent";
  const s = nativeSpan(r, config);
  assert.equal(s.attributes[SC.FI_USAGE_CONTRIBUTION], "none");
  assert.equal(s.parentSpanContext.spanId, "c".repeat(16));
  for (const key of [
    SC.LLM_TOKEN_COUNT_PROMPT,
    SC.LLM_TOKEN_COUNT_COMPLETION,
    SC.LLM_TOKEN_COUNT_TOTAL,
    SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
    SC.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
  ])
    assert.equal(s.attributes[key], undefined);
  assert.equal(
    s.attributes["fi.agent.aggregate.gen_ai.usage.input_tokens"],
    30,
  );
  const child = nativeSpan(raw(), config);
  const all = [s, child];
  assert.equal(
    all.reduce(
      (n, span) => n + (span.attributes[SC.LLM_TOKEN_COUNT_PROMPT] ?? 0),
      0,
    ),
    30,
  );
  assert.equal(
    all.reduce(
      (n, span) => n + (span.attributes[SC.LLM_TOKEN_COUNT_COMPLETION] ?? 0),
      0,
    ),
    6,
  );
  for (const key of [
    "gen_ai.usage.cost",
    "gen_ai.usage.cost_usd",
    "github.copilot.cost",
  ]) {
    r.attributes[key] = 2;
    const parent = nativeSpan(r, config);
    assert.equal(parent.attributes[key], undefined);
    assert.equal(parent.attributes[`fi.agent.aggregate.${key}`], 2);
  }
});
test("nonzero process exit marks tools failed and suppresses native and FI output aliases", () => {
  for (const code of [17, -1]) {
    const r = raw();
    r.attributes["gen_ai.operation.name"] = "execute_tool";
    r.attributes["gen_ai.tool.call.result"] = "PRIVATE_TOOL_ERROR_DETAIL";
    r.attributes["process.exit.code"] = code;
    r.status = { code: 0 };
    const s = nativeSpan(r, { ...config, hideErrorDetails: true });
    assert.equal(s.status.code, 2);
    assert.equal(s.attributes["process.exit.code"], code);
    assert.equal(s.attributes["gen_ai.tool.call.result"], undefined);
    assert.equal(s.attributes[SC.OUTPUT_VALUE], undefined);
    assert.ok(!JSON.stringify(s).includes("PRIVATE_TOOL_ERROR_DETAIL"));
    const visible = nativeSpan(r, config);
    assert.equal(visible.status.code, 2);
    assert.equal(
      visible.attributes[SC.OUTPUT_VALUE],
      "PRIVATE_TOOL_ERROR_DETAIL",
    );
    r.attributes["process.exit.code"] = 0;
    assert.equal(
      nativeSpan(r, { ...config, hideErrorDetails: true }).attributes[
        SC.OUTPUT_VALUE
      ],
      "PRIVATE_TOOL_ERROR_DETAIL",
    );
  }
});
test("reject malformed spans, non-agent records and non-finite timestamps", () => {
  assert.equal(nativeSpan({ ...raw(), traceId: "fake" }, config), undefined);
  assert.equal(nativeSpan({ ...raw(), endTime: [0, 0] }, config), undefined);
  assert.equal(nativeSpan({ ...raw(), type: "metric" }, config), undefined);
  assert.equal(nativeSpan({ ...raw(), attributes: {} }, config), undefined);
});
test("incremental native reader bounds giant lines and recovers at next newline", () => {
  const records = [];
  let dropped = 0;
  const lines = new NativeLines(
    (r) => records.push(r),
    () => dropped++,
  );
  lines.push(Buffer.from('{"ok":'));
  lines.push(Buffer.from("1}\n"));
  lines.push(Buffer.alloc(MAX_LINE_BYTES + 1, 120));
  lines.push(Buffer.from('tail\n{"ok":2}\nno-json\n'));
  lines.end();
  assert.deepEqual(records, [{ ok: 1 }, { ok: 2 }]);
  assert.equal(dropped, 2);
});
test("environment isolates credentials, forces both privacy variables and removes competing exporters", () => {
  const source = {
    PATH: "/bin",
    COPILOT_PROVIDER_BASE_URL: "http://127.0.0.1:8000",
    COPILOT_PROVIDER_API_KEY: "local-dummy",
    FI_API_KEY: "test-fi-api",
    FI_SECRET_KEY: "test-fi-secret",
    OTEL_EXPORTER_OTLP_HEADERS: "old-secret",
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "https://unwanted.invalid",
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT: "SPAN_AND_EVENT",
    COPILOT_OTEL_EXPORTER_TYPE: "otlp-http",
  };
  const env = childEnvironment(source, false, "/private/native.fifo");
  assert.equal(env.FI_API_KEY, undefined);
  assert.equal(env.FI_SECRET_KEY, undefined);
  assert.equal(env.OTEL_EXPORTER_OTLP_HEADERS, undefined);
  assert.equal(env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, undefined);
  assert.equal(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT, "NO_CONTENT");
  assert.equal(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT, "false");
  assert.equal(env.COPILOT_OTEL_EXPORTER_TYPE, "file");
  assert.equal(env.COPILOT_PROVIDER_API_KEY, "local-dummy");
  assert.equal(source.FI_API_KEY, "test-fi-api");
  assert.equal(childEnvironment(source).OTEL_SDK_DISABLED, "true");
  assert.equal(copilotAdapter.requiresWorker, false);
});
test("recorded native Copilot fixture retains the real trace topology with privacy off", () => {
  const records = fs
    .readFileSync(
      path.join(__dirname, "fixtures/copilot-cli-1.0.98-default.jsonl"),
      "utf8",
    )
    .trim()
    .split("\n")
    .map(JSON.parse);
  const spans = records
    .map((r) => nativeSpan(r, { ...config, captureContent: false }))
    .filter(Boolean);
  assert.ok(spans.length >= 4);
  const ids = new Set(spans.map((s) => s.spanContext().spanId));
  assert.ok(
    spans.some((s) => s.attributes["gen_ai.operation.name"] === "execute_tool"),
  );
  for (const s of spans)
    if (s.parentSpanContext) assert.ok(ids.has(s.parentSpanContext.spanId));
});

test("hidden outputs and errors never reappear through native history or FI input aliases", () => {
  for (const setting of ["hideOutputs", "hideErrorDetails"]) {
    for (const operation of ["chat", "invoke_agent"]) {
      const r = raw();
      r.status = { code: 0 };
      r.attributes["gen_ai.operation.name"] = operation;
      r.attributes["gen_ai.input.messages"] = JSON.stringify([
        {
          role: "user",
          parts: [{ type: "text", content: "allowed prompt" }],
          extra: "PRIVATE_TOOL_ERROR_DETAIL",
        },
        {
          role: "tool",
          parts: [
            {
              type: "tool_call_response",
              id: "call-1",
              response: "PRIVATE_TOOL_ERROR_DETAIL",
            },
          ],
        },
        {
          role: "assistant",
          parts: [{ type: "text", content: "PRIVATE_TOOL_ERROR_DETAIL" }],
        },
        {
          role: "user",
          parts: [
            {
              type: "tool_call_response",
              id: "call-1",
              response: "PRIVATE_TOOL_ERROR_DETAIL",
            },
          ],
        },
        {
          role: "user",
          parts: [{ type: "unknown", content: "PRIVATE_TOOL_ERROR_DETAIL" }],
        },
      ]);
      const s = nativeSpan(r, { ...config, [setting]: true });
      assert.ok(!JSON.stringify(s).includes("PRIVATE_TOOL_ERROR_DETAIL"));
      assert.match(s.attributes[SC.INPUT_VALUE], /allowed prompt/);
      assert.equal(
        s.attributes[SC.INPUT_VALUE],
        s.attributes["gen_ai.input.messages"],
      );
      assert.match(
        JSON.stringify(nativeSpan(r, config)),
        /PRIVATE_TOOL_ERROR_DETAIL/,
      );
      for (const history of [
        "PRIVATE_TOOL_ERROR_DETAIL",
        '{"unknown":"PRIVATE_TOOL_ERROR_DETAIL"}',
      ]) {
        r.attributes["gen_ai.input.messages"] = history;
        const opaque = nativeSpan(r, { ...config, [setting]: true });
        assert.equal(opaque.attributes[SC.INPUT_VALUE], undefined);
        assert.equal(opaque.attributes["gen_ai.input.messages"], undefined);
      }
    }
  }
});

test("host auto-updates within the qualified major line remain supported with record shape checks", () => {
  for (const version of ["1.0.98", "1.0.106", "1.0.107", "1.2.0"])
    assert.equal(supportedHostVersion(version), true);
  for (const version of [
    undefined,
    "2.0.0",
    "0.99.0",
    "1.0.106-preview",
    "arbitrary",
  ])
    assert.equal(supportedHostVersion(version), false);
  assert.equal(
    nativeSpan(
      {
        ...raw(),
        attributes: { "gen_ai.operation.name": "new_unknown_operation" },
      },
      config,
    ),
    undefined,
  );
});

test("directory-changing short options are rejected before launching the host", async () => {
  for (const option of [
    "-C",
    "-C=/other/project",
    "-C/other/project",
    "-Crelative",
    "-C../child",
  ])
    assert.equal(await runCopilot({}, [option]), 2, option);
});

test("tool.name and explicit request-dollar costs use backend-readable semantic attributes", () => {
  const r = raw();
  r.attributes["gen_ai.operation.name"] = "execute_tool";
  r.attributes["gen_ai.tool.name"] = "bash";
  r.attributes["gen_ai.tool.call.id"] = "call-1";
  assert.equal(nativeSpan(r, config).attributes[SC.TOOL_NAME], "bash");
  assert.equal(nativeSpan(r, config).attributes[SC.TOOL_CALL_ID], "call-1");
  assert.equal(
    nativeSpan(r, config).attributes["gen_ai.tool.call.id"],
    "call-1",
  );
  r.attributes["gen_ai.operation.name"] = "chat";
  r.attributes["gen_ai.usage.cost_usd"] = 0.12;
  assert.equal(nativeSpan(r, config).attributes["gen_ai.cost.total"], 0.12);
  assert.equal(nativeSpan(r, config).attributes[SC.FI_COST_KIND], "provider_reported");
  assert.equal(nativeSpan(r, config).attributes[SC.FI_USAGE_SCOPE], "request");
  r.attributes["gen_ai.cost.total"] = 0.1;
  assert.equal(nativeSpan(r, config).attributes["gen_ai.cost.total"], 0.1);
  r.attributes["gen_ai.operation.name"] = "invoke_agent";
  const root = nativeSpan(r, config);
  assert.equal(root.attributes["gen_ai.cost.total"], undefined);
  assert.equal(root.attributes["fi.agent.aggregate.gen_ai.cost.total"], 0.1);
  delete r.attributes["gen_ai.cost.total"];
  delete r.attributes["gen_ai.usage.cost_usd"];
  r.attributes["gen_ai.operation.name"] = "chat";
  r.attributes["github.copilot.cost"] = 3;
  r.attributes["gen_ai.usage.cost"] = 3;
  assert.equal(
    nativeSpan(r, config).attributes["gen_ai.cost.total"],
    undefined,
  );
});

test("measured native request TTFT uses milliseconds, preserving zero but not unknowns or aggregates", () => {
  for (const value of [
    0,
    0.131847712,
    0.000332559,
    -1,
    NaN,
    Infinity,
    1e308,
    undefined,
    "0.2",
  ]) {
    const r = raw();
    r.attributes["gen_ai.response.time_to_first_chunk"] = value;
    const valid =
      typeof value === "number" && value >= 0 && Number.isFinite(value * 1000);
    for (const captureContent of [true, false])
      assert.equal(
        nativeSpan(r, { ...config, captureContent }).attributes[
          SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN
        ],
        valid ? value * 1000 : undefined,
      );
    r.attributes["gen_ai.operation.name"] = "invoke_agent";
    assert.equal(
      nativeSpan(r, config).attributes[SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN],
      undefined,
    );
  }
});

test("only native user aborts mark a request and its exact enclosing turn interrupted in every privacy mode", () => {
  for (const policy of [
    {},
    { captureContent: false },
    { hideInputs: true },
    { hideOutputs: true },
    { hideErrorDetails: true },
  ]) {
    const r = raw();
    r.status = { code: 0 };
    r.events = Array.from({ length: 40 }, () => ({ name: "exception" }));
    r.events.push({
      name: "github.copilot.session.abort",
      time: [2, 0],
      attributes: {
        "github.copilot.abort_reason": "user_initiated",
        "exception.message": "PRIVATE_ABORT_TEXT",
        arbitrary: "PRIVATE_ABORT_TEXT",
      },
    });
    const normalizer = new NativeSpans({ ...config, ...policy });
    const request = normalizer.accept(r);
    assert.equal(request.attributes[SC.FI_AGENT_OUTCOME], "interrupted");
    assert.equal(
      request.status.code,
      0,
      "preserve native status independently from cancellation",
    );
    assert.equal(request.events.length, 32);
    assert.deepEqual(request.events.at(-1), {
      name: "github.copilot.session.abort",
      time: [2, 0],
      attributes: { "github.copilot.abort_reason": "user_initiated" },
    });
    assert.ok(!JSON.stringify(request).includes("PRIVATE_ABORT_TEXT"));
    const parent = {
      ...raw(),
      spanId: "c".repeat(16),
      parentSpanId: null,
      events: [],
      status: { code: 0 },
      attributes: { "gen_ai.operation.name": "invoke_agent" },
    };
    assert.equal(
      normalizer.accept({ ...parent, traceId: "d".repeat(32) }).attributes[
        SC.FI_AGENT_OUTCOME
      ],
      undefined,
    );
    assert.equal(
      normalizer.accept(parent).attributes[SC.FI_AGENT_OUTCOME],
      "interrupted",
    );
    assert.equal(
      normalizer.accept(parent).attributes[SC.FI_AGENT_OUTCOME],
      undefined,
      "consume parent correlation once",
    );
    for (const reason of [undefined, "PRIVATE_ABORT_REASON", "timeout"]) {
      r.events[40].attributes["github.copilot.abort_reason"] = reason;
      const unknown = nativeSpan(r, { ...config, ...policy });
      assert.equal(unknown.attributes[SC.FI_AGENT_OUTCOME], undefined);
      assert.ok(!JSON.stringify(unknown).includes("PRIVATE_ABORT_REASON"));
    }
    r.events = [];
    r.attributes["gen_ai.response.finish_reasons"] = ["error"];
    r.status = { code: 2 };
    assert.equal(
      normalizer.accept(r).attributes[SC.FI_AGENT_OUTCOME],
      undefined,
    );
  }
});

test("real version-banner punctuation is stripped without accepting prerelease suffixes", () => {
  for (const banner of [
    "GitHub Copilot CLI 1.0.106.\n",
    "GitHub Copilot CLI 1.0.107\n",
  ])
    assert.equal(supportedHostVersion(hostVersionFromOutput(banner)), true);
  for (const banner of [
    "GitHub Copilot CLI 1.0.107-preview.\n",
    "GitHub Copilot CLI 2.0.0.\n",
    "unrecognized host",
  ])
    assert.equal(supportedHostVersion(hostVersionFromOutput(banner)), false);
});


test("failure result envelopes suppress both native and FI output aliases", () => {
  for (const marker of [
    { isError: true }, { is_error: true }, { success: false },
    { type: "error" }, { status: "error" }, { status: "failed" },
  ]) {
    const r = raw();
    r.status = { code: 0 };
    r.attributes["gen_ai.operation.name"] = "execute_tool";
    r.attributes["gen_ai.tool.call.result"] = JSON.stringify({ ...marker, message: "PRIVATE_ENVELOPE_BODY" });
    const hidden = nativeSpan(r, { ...config, hideErrorDetails: true });
    assert.equal(hidden.status.code, 2);
    assert.equal(hidden.attributes[SC.OUTPUT_VALUE], undefined);
    assert.equal(hidden.attributes["gen_ai.tool.call.result"], undefined);
    assert.ok(!JSON.stringify(hidden).includes("PRIVATE_ENVELOPE_BODY"));
    assert.match(nativeSpan(r, config).attributes[SC.OUTPUT_VALUE], /PRIVATE_ENVELOPE_BODY/);
  }
});

test("final source model failure marks its root ERROR but a later retry recovers", () => {
  for (const recovered of [false, true]) {
    const normalizer = new NativeSpans(config);
    const failure = raw();
    failure.startTime = [1, 0];
    failure.endTime = [2, 0];
    const success = { ...raw(), startTime: [3, 0], endTime: [4, 0], status: { code: 0 } };
    if (recovered) normalizer.accept(success); // Out-of-order receipt must not undo recovery.
    normalizer.accept(failure);
    const parent = { ...raw(), spanId: "c".repeat(16), parentSpanId: null,
      startTime: [0, 0], endTime: [5, 0], status: { code: 0 }, events: [],
      attributes: { "gen_ai.operation.name": "invoke_agent" } };
    const unrelated = normalizer.accept({ ...parent, traceId: "d".repeat(32) });
    assert.equal(unrelated.status.code, 0);
    const result = normalizer.accept(parent);
    assert.equal(result.status.code, recovered ? 0 : 2);
    assert.equal(result.attributes[SC.FI_AGENT_OUTCOME], recovered ? undefined : "error");
    assert.equal(normalizer.accept(parent).status.code, 0, "correlation consumed at finalization");
  }
});


test("inferred fatal root status also hides its native error output", () => {
  const normalizer = new NativeSpans({ ...config, hideErrorDetails: true });
  normalizer.accept(raw());
  const parent = { ...raw(), spanId: "c".repeat(16), parentSpanId: null,
    status: { code: 0 }, events: [], attributes: {
      "gen_ai.operation.name": "invoke_agent",
      "gen_ai.output.messages": "PRIVATE_FATAL_ROOT_BODY",
    } };
  const root = normalizer.accept(parent);
  assert.equal(root.status.code, 2);
  assert.equal(root.attributes[SC.OUTPUT_VALUE], undefined);
  assert.ok(!JSON.stringify(root).includes("PRIVATE_FATAL_ROOT_BODY"));
});
