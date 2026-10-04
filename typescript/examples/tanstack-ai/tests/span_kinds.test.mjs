// Unit tests for futureAgiSpanKinds(), run with `node --test` (the contract
// test runs this file). Fake spans stand in for the SDK span, and run() calls
// the hooks in the order otelMiddleware does at @tanstack/ai 0.64.0:
// attributeEnricher for the root at start, then per model call
// attributeEnricher, the usage attributes and onSpanEnd, then applyRootUsage
// on the root (the sum over calls, or the finish usage when no call reported
// any) just before the root's onSpanEnd.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { GEN_AI_SPAN_KIND, futureAgiSpanKinds } from "../src/tracing.mjs";

// The root-usage move edits the SDK span's attributes object, which is not
// public API. These are the versions it was tested against.
const TESTED_SDK_TRACE = "2.11.0";

/** Resolve `name` the way the package that owns `req` resolves it. */
function packageInfo(req, name) {
  let dir = dirname(req.resolve(name));
  for (;;) {
    const file = join(dir, "package.json");
    if (existsSync(file)) {
      const pkg = JSON.parse(readFileSync(file, "utf8"));
      if (pkg.name === name) return { version: pkg.version, req: createRequire(file) };
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
    sdk: fiCore.req("@opentelemetry/sdk-trace-node"),
  };
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

test("one model call: no kind and no promoted usage on the root", () => {
  const call = usage(11, 7);
  const { root, modelCalls, spans } = run(futureAgiSpanKinds(), [call]);
  assert.equal(root.attributes[GEN_AI_SPAN_KIND], undefined);
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

test("no session.id by default, although TanStack always sets a threadId", () => {
  // chat() generates thread-<ms>-<random> when the caller passes none, so
  // ctx.threadId alone does not say the caller has a conversation.
  const { spans } = run(futureAgiSpanKinds(), [usage(11, 7)]);
  for (const span of spans) {
    assert.equal(span.attributes["session.id"], undefined);
  }
});
