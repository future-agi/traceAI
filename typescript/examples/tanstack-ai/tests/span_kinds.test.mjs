// Unit tests for futureAgiSpanKinds() and the tracer provider registration,
// run with `node --test` (the contract test runs this file). Fake spans stand
// in for the SDK span, and run() calls
// the hooks in the order otelMiddleware does at @tanstack/ai 0.64.0 to 0.69.0:
// attributeEnricher for the root at start, then per model call
// attributeEnricher, the usage attributes and onSpanEnd, then applyRootUsage
// on the root (the sum over calls, or the finish usage when no call reported
// any) just before the root's onSpanEnd.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import {
  ERROR_TYPE,
  GEN_AI_SPAN_KIND,
  errorType,
  futureAgiErrorPrivacy,
  futureAgiSpanKinds,
  registerFutureAgiTracing,
  shutdownTraces,
} from "../src/tracing.mjs";

// The root-usage move edits the SDK span's attributes object, which is not
// public API. These are the versions it was tested against.
const TESTED_SDK_TRACE = "2.12.0";

/** Resolve `name` the way the package that owns `req` resolves it. */
function packageInfo(req, name) {
  let dir = dirname(req.resolve(name));
  for (;;) {
    const file = join(dir, "package.json");
    if (existsSync(file)) {
      const pkg = JSON.parse(readFileSync(file, "utf8"));
      if (pkg.name === name) {
        return { version: pkg.version, engines: pkg.engines, req: createRequire(file) };
      }
    }
    const parent = dirname(dir);
    if (parent === dir) throw new Error(`package.json not found for ${name}`);
    dir = parent;
  }
}

/** The SDK chain behind @traceai/fi-core's register() provider. */
function fiCoreSdk() {
  const fiCore = packageInfo(createRequire(import.meta.url), "@traceai/fi-core");
  const node = packageInfo(fiCore.req, "@opentelemetry/sdk-trace-node");
  const base = packageInfo(node.req, "@opentelemetry/sdk-trace-base");
  const impl = packageInfo(base.req, "@opentelemetry/sdk-trace");
  return {
    versions: [node.version, base.version, impl.version],
    nodeRanges: {
      "@opentelemetry/sdk-trace-node": node.engines?.node,
      "@opentelemetry/sdk-trace-base": base.engines?.node,
      "@opentelemetry/sdk-trace": impl.engines?.node,
    },
    sdk: fiCore.req("@opentelemetry/sdk-trace-node"),
  };
}

/** `>=20`, `20.6.0` -> [20, 6, 0]. */
function parseVersion(text) {
  const parts = text.replace(/^>=\s*/, "").trim().split(".").map(Number);
  assert.ok(parts.length <= 3 && parts.every(Number.isInteger), text);
  return [0, 1, 2].map((i) => parts[i] ?? 0);
}

/** Whether `version` is in an engines range made of `^x.y.z` and `>=x.y.z` alternatives. */
function inNodeRange(version, range) {
  const compare = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
  return range.split("||").some((alternative) => {
    const match = alternative.trim().match(/^(\^|>=)\s*(\d+\.\d+\.\d+)$/);
    assert.ok(match, `unsupported engines range: ${range}`);
    const floor = parseVersion(match[2]);
    if (compare(version, floor) < 0) return false;
    return match[1] === ">=" || version[0] === floor[0];
  });
}

class FakeSpan {
  constructor() {
    this.attributes = {};
  }
  setAttribute(key, value) {
    this.attributes[key] = value;
    return this;
  }
  setAttributes(attributes) {
    for (const [key, value] of Object.entries(attributes ?? {})) {
      this.setAttribute(key, value);
    }
    return this;
  }
}

function usage(input, output, extra = {}) {
  return {
    "gen_ai.usage.input_tokens": input,
    "gen_ai.usage.output_tokens": output,
    "gen_ai.usage.total_tokens": input + output,
    ...extra,
  };
}

function run(
  hooks,
  calls,
  { finishUsage = {}, rootExtra = {}, ctx = {}, newSpan = () => new FakeSpan() } = {},
) {
  const runCtx = { threadId: "thread-1700000000000-abc1234", ...ctx };
  const root = newSpan("chat").setAttributes(
    hooks.attributeEnricher({ kind: "chat", ctx: runCtx }),
  );
  const modelCalls = calls.map((callUsage, iteration) => {
    const info = { kind: "iteration", ctx: runCtx, iteration };
    const span = newSpan(`chat #${iteration}`).setAttributes(hooks.attributeEnricher(info));
    span.setAttributes(callUsage ?? {});
    hooks.onSpanEnd(info, span);
    return span;
  });
  const summed = {};
  for (const callUsage of calls) {
    for (const [key, value] of Object.entries(callUsage ?? {})) {
      summed[key] = (summed[key] ?? 0) + value;
    }
  }
  root.setAttributes(Object.keys(summed).length > 0 ? summed : finishUsage);
  root.setAttributes(rootExtra);
  hooks.onSpanEnd({ kind: "chat", ctx: runCtx }, root);
  return { root, modelCalls, spans: [root, ...modelCalls] };
}

function traceSum(spans, key) {
  return spans.reduce((total, span) => total + (span.attributes[key] ?? 0), 0);
}

function promotedKeys(attributes) {
  return Object.keys(attributes).filter(
    (key) => key.startsWith("gen_ai.usage.") || key.startsWith("gen_ai.cost."),
  );
}

function rootUsageKeys(attributes) {
  return Object.keys(attributes).filter((key) =>
    key.startsWith("tanstack.ai.root_usage."),
  );
}

test("no model call reported usage: the root keeps its usage", () => {
  const finishUsage = usage(30, 12);
  const { root, spans } = run(futureAgiSpanKinds(), [null, null], { finishUsage });
  for (const [key, value] of Object.entries(finishUsage)) {
    assert.equal(root.attributes[key], value, key);
  }
  assert.deepEqual(rootUsageKeys(root.attributes), []);
  assert.equal(traceSum(spans, "gen_ai.usage.input_tokens"), 30);
  assert.equal(traceSum(spans, "gen_ai.usage.output_tokens"), 12);
  assert.equal(traceSum(spans, "gen_ai.usage.total_tokens"), 42);
});

test("one model call: CHAIN and no promoted usage on the root", () => {
  // Without a kind, fi-collector stores the root as observation_type "unknown".
  const call = usage(11, 7);
  const { root, modelCalls, spans } = run(futureAgiSpanKinds(), [call]);
  assert.equal(root.attributes[GEN_AI_SPAN_KIND], "CHAIN");
  assert.equal(modelCalls[0].attributes[GEN_AI_SPAN_KIND], "LLM");
  assert.deepEqual(promotedKeys(root.attributes), []);
  assert.equal(root.attributes["tanstack.ai.root_usage.input_tokens"], 11);
  for (const [key, value] of Object.entries(call)) {
    assert.equal(traceSum(spans, key), value, key);
  }
});

test("two model calls: every promoted key leaves the root and each counts once", () => {
  const first = usage(11, 7, { "gen_ai.usage.cost": 0.25 });
  const second = usage(23, 5, {
    "gen_ai.usage.cost": 0.5,
    "gen_ai.usage.cache_read.input_tokens": 3,
    "gen_ai.usage.cache_creation.input_tokens": 4,
    "gen_ai.usage.reasoning.output_tokens": 2,
  });
  const { root, spans } = run(futureAgiSpanKinds(), [first, second], {
    rootExtra: { "gen_ai.cost.total": 0.75 },
  });
  assert.equal(root.attributes[GEN_AI_SPAN_KIND], "AGENT");
  assert.deepEqual(promotedKeys(root.attributes), []);

  const expected = {
    input_tokens: 34,
    output_tokens: 12,
    total_tokens: 46,
    cost: 0.75,
    "cache_read.input_tokens": 3,
    "cache_creation.input_tokens": 4,
    "reasoning.output_tokens": 2,
  };
  for (const [suffix, value] of Object.entries(expected)) {
    assert.equal(root.attributes[`tanstack.ai.root_usage.${suffix}`], value, suffix);
    assert.equal(traceSum(spans, `gen_ai.usage.${suffix}`), value, suffix);
  }
  // gen_ai.cost.<x> keeps its own namespace, so it cannot collide with
  // gen_ai.usage.cost.
  assert.equal(root.attributes["tanstack.ai.root_usage.cost.total"], 0.75);
  assert.equal(traceSum(spans, "gen_ai.cost.total"), 0);
});

test(`fi-core's provider creates spans with @opentelemetry/sdk-trace ${TESTED_SDK_TRACE}`, () => {
  // sdk-trace-node, sdk-trace-base and sdk-trace (where SpanImpl lives).
  // package.json "overrides" pins sdk-trace-node, which pins the other two
  // exactly. A different version needs the tests below re-run and this
  // constant moved.
  assert.deepEqual(fiCoreSdk().versions, [
    TESTED_SDK_TRACE,
    TESTED_SDK_TRACE,
    TESTED_SDK_TRACE,
  ]);
});

test("package.json engines starts at a Node version the pinned SDK supports", () => {
  // The lowest Node the example allows must be inside every pinned SDK
  // package's own engines range (2.12.0: ^18.19.0 || >=20.6.0).
  const example = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  const range = example.engines.node;
  assert.match(range, /^>=\s*\d+(\.\d+){0,2}$/, `engines.node is not a >= floor: ${range}`);
  const floor = parseVersion(range);
  for (const [name, sdkRange] of Object.entries(fiCoreSdk().nodeRanges)) {
    assert.ok(sdkRange, `${name} declares no engines.node`);
    assert.ok(
      inNodeRange(floor, sdkRange),
      `engines.node ${range} allows Node ${floor.join(".")}, outside ${name}'s ${sdkRange}`,
    );
  }
});

function sdkRun(spanLimits) {
  const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = fiCoreSdk().sdk;
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
    ...(spanLimits ? { spanLimits } : {}),
  });
  const tracer = provider.getTracer("span-kinds-test");
  const calls = [
    usage(11, 7),
    usage(23, 5, { "gen_ai.usage.cache_read.input_tokens": 3 }),
  ];
  const { root, modelCalls } = run(futureAgiSpanKinds(), calls, {
    newSpan: (name) => tracer.startSpan(name),
  });
  for (const span of [...modelCalls, root]) span.end();
  const exported = exporter.getFinishedSpans();
  return {
    root: exported.find((span) => span.name === "chat"),
    modelCalls: exported.filter((span) => span.name !== "chat"),
  };
}

test("the move reaches the exported SDK span", () => {
  const { root, modelCalls } = sdkRun();
  assert.deepEqual(promotedKeys(root.attributes), []);
  assert.equal(root.attributes["tanstack.ai.root_usage.input_tokens"], 34);
  assert.equal(root.attributes["tanstack.ai.root_usage.cache_read.input_tokens"], 3);
  assert.equal(traceSum([root, ...modelCalls], "gen_ai.usage.input_tokens"), 34);
  assert.equal(traceSum([root, ...modelCalls], "gen_ai.usage.total_tokens"), 46);
});

test("a tight attribute count limit never leaves usage counted twice", () => {
  // delete does not free a slot in SpanImpl's attribute count, so with the
  // root at its limit the tanstack.ai.root_usage.* copies can be dropped.
  // The promoted keys still leave the root, and each model call keeps its
  // own usage, so the trace total stays right.
  const { root, modelCalls } = sdkRun({ attributeCountLimit: 5 });
  assert.deepEqual(promotedKeys(root.attributes), []);
  assert.equal(traceSum([root, ...modelCalls], "gen_ai.usage.input_tokens"), 34);
  assert.equal(traceSum([root, ...modelCalls], "gen_ai.usage.output_tokens"), 12);
});

test("threadIdAsSession: every span carries the caller's thread id as session.id", () => {
  const hooks = futureAgiSpanKinds({ threadIdAsSession: true });
  const ctx = { threadId: "conversation-42" };
  const { spans } = run(hooks, [usage(11, 7), usage(23, 5)], { ctx });
  assert.equal(spans.length, 3);
  for (const span of spans) {
    assert.equal(span.attributes["session.id"], "conversation-42");
  }
  const tool = hooks.attributeEnricher({
    kind: "tool",
    ctx,
    toolCallId: "call_1",
    toolName: "get_weather",
    iteration: 0,
  });
  assert.deepEqual(tool, { [GEN_AI_SPAN_KIND]: "TOOL", "session.id": "conversation-42" });
});

test("registerFutureAgiTracing() leaves the global tracer provider alone", async () => {
  // The recipe hands its tracer to otelMiddleware, so it never needs the
  // global provider. Taking the global slot would compete with an app's own
  // OpenTelemetry setup or send that app's other spans to Future AGI.
  // Placeholder settings only: register() makes no request, and no span is
  // created here.
  process.env.FI_API_KEY = "placeholder-fi-api-key";
  process.env.FI_SECRET_KEY = "placeholder-fi-secret-key";
  process.env.FI_PROJECT_NAME = "span-kinds-test";
  const provider = registerFutureAgiTracing();
  try {
    assert.notEqual(trace.getTracerProvider().getDelegate(), provider);
    // The slot is still free for the app's own provider.
    const { BasicTracerProvider } = fiCoreSdk().sdk;
    assert.equal(trace.setGlobalTracerProvider(new BasicTracerProvider()), true);
  } finally {
    trace.disable();
    await shutdownTraces(provider);
  }
});

test("no session.id by default, although TanStack always sets a threadId", () => {
  // chat() generates thread-<ms>-<random> when the caller passes none, so
  // ctx.threadId alone does not say the caller has a conversation.
  const { spans } = run(futureAgiSpanKinds(), [usage(11, 7)]);
  for (const span of spans) {
    assert.equal(span.attributes["session.id"], undefined);
  }
});

test("gen_ai.provider.name from the adapter name, so Future AGI shows the vendor", () => {
  // TanStack copies adapter.name to gen_ai.system ("openai-chat" for openaiChatCompletions);
  // fi-collector reads gen_ai.provider.name first. Adapter names from the published adapters.
  const cases = [
    ["openai-chat", "openai"],
    ["openai", "openai"],
    ["anthropic", "anthropic"],
    ["gemini", "google"],
    ["gemini-text-interactions", "google"],
    ["grok", "xai"],
    ["groq", "groq"],
    ["ollama", "ollama"],
  ];
  for (const [adapter, provider] of cases) {
    const { root, modelCalls } = run(futureAgiSpanKinds(), [usage(1, 1)], { ctx: { provider: adapter } });
    assert.equal(modelCalls[0].attributes["gen_ai.provider.name"], provider, adapter);
    assert.equal(root.attributes["gen_ai.provider.name"], provider, adapter);
  }
});

test("an adapter name with no known vendor leaves gen_ai.provider.name unset", () => {
  for (const adapter of ["openrouter", "openrouter-responses", "my-adapter", "constructor", undefined]) {
    const { root, modelCalls } = run(futureAgiSpanKinds(), [usage(1, 1)], { ctx: { provider: adapter } });
    assert.equal(modelCalls[0].attributes["gen_ai.provider.name"], undefined, String(adapter));
    assert.equal(root.attributes["gen_ai.provider.name"], undefined, String(adapter));
  }
});

function privacyRun(options, act) {
  const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = fiCoreSdk().sdk;
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = futureAgiErrorPrivacy(provider.getTracer("error-privacy-test"), options);
  act(tracer);
  return exporter.getFinishedSpans();
}

function failingSpan(tracer, error, message = error.message) {
  // What otelMiddleware does on a failed model call or tool.
  const span = tracer.startSpan("failing");
  span.recordException(error);
  span.setStatus({ code: SpanStatusCode.ERROR, message });
  span.end();
}

const SECRET_ERROR = "MARKER-7a1c cannot read /Users/someone/private/notes.json";

test("content off: an error exports its type, never its message or stack", () => {
  const error = new Error(SECRET_ERROR);
  assert.ok(error.stack.includes("span_kinds.test.mjs"));
  const [span] = privacyRun({}, (tracer) => failingSpan(tracer, error));
  assert.equal(span.status.code, SpanStatusCode.ERROR);
  assert.equal(span.status.message, "Error");
  assert.equal(span.attributes[ERROR_TYPE], "Error");
  assert.deepEqual(span.events.map((event) => [event.name, event.attributes]), [
    ["exception", { "exception.type": "Error" }],
  ]);
  const exported = JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status });
  for (const fragment of ["MARKER-7a1c", "/Users/someone", "span_kinds.test.mjs", "stacktrace"]) {
    assert.ok(!exported.includes(fragment), fragment);
  }
});

test("error type: the provider's code first, then the class name", () => {
  const rateLimited = Object.assign(new Error("slow down"), { code: 429 });
  const overflow = Object.assign(new Error("too long"), { code: "context_overflow" });
  class ToolTimeout extends Error {
    name = "ToolTimeout";
  }
  assert.equal(errorType(rateLimited), "429");
  assert.equal(errorType(overflow), "context_overflow");
  assert.equal(errorType(new ToolTimeout("x")), "ToolTimeout");
  assert.equal(errorType("a thrown string"), "Error");
  assert.equal(errorType(undefined), "Error");
  const [span] = privacyRun({}, (tracer) => failingSpan(tracer, overflow));
  assert.equal(span.status.message, "context_overflow");
  assert.equal(span.attributes[ERROR_TYPE], "context_overflow");
});

test("cancelled and OK statuses pass through unchanged", () => {
  const spans = privacyRun({}, (tracer) => {
    const aborted = tracer.startSpan("aborted");
    aborted.setStatus({ code: SpanStatusCode.ERROR, message: "cancelled" });
    aborted.end();
    const ok = tracer.startSpan("ok");
    ok.setStatus({ code: SpanStatusCode.OK });
    ok.end();
  });
  const byName = Object.fromEntries(spans.map((span) => [span.name, span]));
  assert.deepEqual(byName.aborted.status, { code: SpanStatusCode.ERROR, message: "cancelled" });
  assert.equal(byName.ok.status.code, SpanStatusCode.OK);
  assert.equal(byName.aborted.events.length, 0);
});

test("content opt-in: the error message is exported through redact, a stack never", () => {
  const redact = (text) => text.replace(/\/Users\/[^ ]+/g, "[path]");
  const [span] = privacyRun({ captureContent: true, redact }, (tracer) =>
    failingSpan(tracer, new Error(SECRET_ERROR)),
  );
  assert.equal(span.status.message, "MARKER-7a1c cannot read [path]");
  assert.deepEqual(span.events.map((event) => event.attributes), [
    { "exception.type": "Error", "exception.message": "MARKER-7a1c cannot read [path]" },
  ]);
  const throwing = () => {
    throw new Error("redactor bug");
  };
  const [failed] = privacyRun({ captureContent: true, redact: throwing }, (tracer) =>
    failingSpan(tracer, new Error(SECRET_ERROR)),
  );
  assert.equal(failed.status.message, "[redaction_failed]");
  assert.equal(failed.events[0].attributes["exception.message"], "[redaction_failed]");
  assert.ok(!JSON.stringify(failed.events).includes("stacktrace"));
});

test("wrapped spans still parent, take attributes and feed the root usage move", () => {
  const spans = privacyRun({}, (tracer) => {
    const root = tracer.startSpan("chat");
    const child = tracer.startSpan("chat #0", {}, trace.setSpan(context.active(), root));
    child.setAttributes(usage(11, 7)).end();
    const hooks = futureAgiSpanKinds();
    hooks.onSpanEnd({ kind: "iteration", ctx: root, iteration: 0 }, child);
    root.setAttributes(usage(11, 7));
    hooks.onSpanEnd({ kind: "chat", ctx: root }, root);
    assert.equal(root.isRecording(), true);
    root.end();
  });
  const byName = Object.fromEntries(spans.map((span) => [span.name, span]));
  assert.equal(byName["chat #0"].parentSpanContext.spanId, byName.chat.spanContext().spanId);
  assert.deepEqual(promotedKeys(byName.chat.attributes), []);
  assert.equal(byName.chat.attributes["tanstack.ai.root_usage.input_tokens"], 11);
  assert.equal(byName["chat #0"].attributes["gen_ai.usage.input_tokens"], 11);
});
