// Unit tests for futureAgiSpanKinds(), run with `node --test` (the contract
// test runs this file). Fake spans stand in for the SDK span, and run() calls
// the hooks in the order otelMiddleware does at @tanstack/ai 0.64.0:
// attributeEnricher for the root at start, then per model call
// attributeEnricher, the usage attributes and onSpanEnd, then applyRootUsage
// on the root (the sum over calls, or the finish usage when no call reported
// any) just before the root's onSpanEnd.
import assert from "node:assert/strict";
import { test } from "node:test";
import { GEN_AI_SPAN_KIND, futureAgiSpanKinds } from "../src/tracing.mjs";

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

function run(hooks, calls, { finishUsage = {}, rootExtra = {}, ctx = {} } = {}) {
  const runCtx = { threadId: "thread-1700000000000-abc1234", ...ctx };
  const root = new FakeSpan().setAttributes(
    hooks.attributeEnricher({ kind: "chat", ctx: runCtx }),
  );
  const modelCalls = calls.map((callUsage, iteration) => {
    const info = { kind: "iteration", ctx: runCtx, iteration };
    const span = new FakeSpan().setAttributes(hooks.attributeEnricher(info));
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
