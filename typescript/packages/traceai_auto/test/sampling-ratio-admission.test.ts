import { ROOT_CONTEXT, trace, TraceFlags, createTraceState, diag, DiagLogLevel, defaultTextMapGetter, defaultTextMapSetter, type Span, type SpanContext } from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import * as currentSdk from "@opentelemetry/sdk-trace-base";
import type { ReadableSpan, Sampler } from "@opentelemetry/sdk-trace-base";
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, TraceIdRatioBasedSampler, ParentBasedSampler, SamplingDecision } = currentSdk;
import { FITracer, privacyFilter } from "@traceai/fi-core";
import { samplingRatioAdmission } from "../src/sampling-ratio";

const key = "fi.sampling.ratio";
const keptId = "00000000000000000000000000000001";
const parent = (entry?: string) => trace.setSpanContext(ROOT_CONTEXT, {
  traceId: keptId, spanId: "1234567890123456", traceFlags: TraceFlags.SAMPLED,
  traceState: entry === undefined ? undefined : createTraceState(`fi=${entry}`),
});

describe("SDK admission", () => {

test.each([1, 16, 128])("owned SDK admission retains the ratio at %s slots, including integer-like keys", async limit => {
  const admission = samplingRatioAdmission({ sampler: new TraceIdRatioBasedSampler(0.25), ratio: { rate: 0.25, parentBased: false } });
  const exporter = new InMemorySpanExporter();
  const exporterProcessor = new SimpleSpanProcessor(exporter);
  const provider = new BasicTracerProvider({ sampler: admission.sampler, spanLimits: { attributeCountLimit: limit },
    spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }, exporterProcessor] });
  try {
    const attributes = Object.freeze(Object.fromEntries(Array.from({ length: limit }, (_, i) => [String(i), "value"])));
    const span = provider.getTracer("test").startSpan("test", { attributes }, parent());
    span.setAttribute("later", "cannot exceed the cap");
    span.end();
    await provider.forceFlush();
    const exported = exporter.getFinishedSpans()[0];
    expect(exported.attributes[key]).toBe(0.25);
    expect(Object.keys(exported.attributes)).toHaveLength(limit);
    expect(exported.attributes.later).toBeUndefined();
    expect(exported.droppedAttributesCount).toBe(2);
    expect(exported.spanContext().traceState?.get("fi")).toBe("r:0.25");
    expect(Object.keys(attributes)).toHaveLength(limit);
    expect(attributes[key]).toBeUndefined();
    expect(privacyFilter().span(exported).attributes[key]).toBe(0.25);
  } finally { await provider.shutdown(); }
});

test.each([0, 1])("parent-based root rate %s admits inherited metadata at a one-slot budget", async rate => {
  const admission = samplingRatioAdmission({ sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(rate) }), ratio: { rate, parentBased: true } });
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ sampler: admission.sampler, spanLimits: { attributeCountLimit: 1 },
    spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }, new SimpleSpanProcessor(exporter)] });
  try {
    const attributes = Object.freeze({ existing: "value" });
    provider.getTracer("test").startSpan("test", { attributes }, parent("r:0.5")).end();
    const exported = exporter.getFinishedSpans()[0];
    expect(exported.attributes).toEqual({ [key]: 0.5 });
    expect(exported.spanContext().traceState?.get("fi")).toBe("r:0.5");
    expect(exported.droppedAttributesCount).toBe(1);
    expect(attributes).toEqual({ existing: "value" });
  } finally { await provider.shutdown(); }
});

test.each([undefined, "r:bad", "r:1"])("missing or invalid inherited ratio %s does not displace an attribute", async entry => {
  const admission = samplingRatioAdmission({ sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(0.25) }), ratio: { rate: 0.25, parentBased: true } });
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ sampler: admission.sampler, spanLimits: { attributeCountLimit: 1 },
    spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }, new SimpleSpanProcessor(exporter)] });
  try {
    provider.getTracer("test").startSpan("test", { attributes: { existing: "value" } }, parent(entry)).end();
    expect(exporter.getFinishedSpans()[0].attributes).toEqual({ existing: "value" });
  } finally { await provider.shutdown(); }
});

test("unknown samplers remain identical and their inputs remain untouched", () => {
  const sampler = { shouldSample: () => ({ decision: SamplingDecision.RECORD_AND_SAMPLED }), toString: () => "custom" };
  expect(samplingRatioAdmission({ sampler }).sampler).toBe(sampler);
});

test.each(["auto", "standalone"])("%s FITracer late writes retain sampler metadata at one slot", async mode => {
  const admission = samplingRatioAdmission({ sampler: new TraceIdRatioBasedSampler(0.25), ratio: { rate: 0.25, parentBased: false } });
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ sampler: admission.sampler, spanLimits: { attributeCountLimit: 1 },
    spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }, new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: (mode === "auto" ? FITracer.autoProvider(provider) : provider).getTracer("test") });
    const span = tracer.startSpan("test", { attributes: { existing: "value" } }, parent());
    span.setAttributes({ "output.value": "answer", "gen_ai.usage.output_tokens": 10 });
    span.end();
    await provider.forceFlush();
    const exported = exporter.getFinishedSpans()[0];
    expect(exported.attributes).toEqual({ [key]: 0.25 });
    expect(privacyFilter().span(exported).attributes[key]).toBe(0.25);
  } finally { await provider.shutdown(); }
});

describe.each([true, false])("fresh roots with invalid parents (parentBased=%s)", parentBased => {
  test.each([
    { field: "traceId", rate: 0.25 }, { field: "spanId", rate: 0.25 },
    { field: "traceId", rate: 1 }, { field: "spanId", rate: 1 },
  ] as const)("zero $field at local rate $rate exports and propagates only local state", async ({ field, rate }) => {
    const original = createTraceState("vendor=stale-old-trace,fi=r:0.5");
    const ctx = trace.setSpanContext(ROOT_CONTEXT, {
      traceId: keptId, spanId: "1234567890123456", traceFlags: TraceFlags.SAMPLED,
      [field]: "0".repeat(field === "traceId" ? 32 : 16), traceState: original,
    });
    const root = new TraceIdRatioBasedSampler(rate);
    const delegate = parentBased ? new ParentBasedSampler({ root }) : root;
    const admission = samplingRatioAdmission({ sampler: delegate, ratio: { rate, parentBased } });
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ sampler: admission.sampler,
      idGenerator: { generateTraceId: () => keptId, generateSpanId: () => "abcdef0123456789" },
      spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }, new SimpleSpanProcessor(exporter)] });
    try {
      const span = provider.getTracer("test").startSpan("fresh root", {}, ctx);
      const carrier: Record<string, string> = {};
      const propagator = new W3CTraceContextPropagator();
      propagator.inject(trace.setSpan(ROOT_CONTEXT, span), carrier, defaultTextMapSetter);
      span.end();
      await provider.forceFlush();
      const exported = exporter.getFinishedSpans()[0];
      expect(exported.parentSpanContext).toBeUndefined();
      expect(exported.spanContext().traceFlags).toBe(TraceFlags.SAMPLED);
      expect(exported.attributes[key]).toBe(rate < 1 ? rate : undefined);
      const expectedState = rate < 1 ? "fi=r:0.25" : undefined;
      expect(exported.spanContext().traceState?.serialize()).toBe(expectedState);
      expect(carrier.tracestate).toBe(expectedState);
      const extracted = trace.getSpanContext(propagator.extract(ROOT_CONTEXT, carrier, defaultTextMapGetter))!;
      expect(extracted.traceState?.serialize()).toBe(expectedState);
      expect(extracted.traceFlags).toBe(TraceFlags.SAMPLED);
      expect(original.serialize()).toBe("vendor=stale-old-trace,fi=r:0.5");
    } finally { await provider.shutdown(); }
  });
});
});

test.each([false, true])("native admission state survives mandatory export privacy (parentBased=%s)", async parentBased => {
  const original = createTraceState("vendor=opaque,fi=r:0.5");
  const ctx = trace.setSpanContext(parent(), { ...trace.getSpanContext(parent())!, traceState: original });
  const root = new TraceIdRatioBasedSampler(0.25);
  const admission = samplingRatioAdmission({ sampler: parentBased ? new ParentBasedSampler({ root }) : root, ratio: { rate: 0.25, parentBased } });
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ sampler: admission.sampler,
    spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }, new SimpleSpanProcessor(exporter)] });
  try {
    provider.getTracer("test").startSpan("privacy", {}, ctx).end();
    const exported = exporter.getFinishedSpans()[0];
    const filtered = privacyFilter().span(exported);
    const expected = parentBased ? "vendor=opaque,fi=r:0.5" : "fi=r:0.25,vendor=opaque";
    expect(exported.spanContext().traceState?.serialize()).toBe(expected);
    // Privacy reconstructs native state and may change member order, not values.
    expect(new Set(filtered.spanContext().traceState?.serialize().split(","))).toEqual(new Set(expected.split(",")));
    expect(filtered.attributes[key]).toBe(parentBased ? 0.5 : 0.25);
    expect(exported.spanContext().traceState).not.toBe(original);
    expect(original.serialize()).toBe("vendor=opaque,fi=r:0.5");
  } finally { await provider.shutdown(); }
});

test("native admission snapshots stay independent across alternating providers", async () => {
  const providers = Array.from({ length: 2 }, () => {
    const admission = samplingRatioAdmission({ sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(1) }), ratio: { rate: 1, parentBased: true } });
    return new BasicTracerProvider({ sampler: admission.sampler, spanLimits: { attributeCountLimit: 1 },
      spanProcessors: [{ ...admission, onEnd() {}, async forceFlush() {}, async shutdown() {} }] });
  });
  try {
    const tracers = providers.map(provider => provider.getTracer("test"));
    const original = createTraceState("vendor=opaque,fi=r:0.5");
    let latest = { ...trace.getSpanContext(parent())!, traceState: original };
    // Keep only the newest context, not ancestor spans. A facade closure chain
    // becomes recursively deeper across providers and eventually overflows.
    for (let i = 0; i < 12000; i++) {
      const span = tracers[i % 2].startSpan("descendant", { attributes: { existing: "ordinary" } }, trace.setSpanContext(ROOT_CONTEXT, latest));
      const context = span.spanContext();
      expect(context.traceState).not.toBe(latest.traceState);
      latest = { ...context, traceState: context.traceState! };
      span.end();
    }
    expect(latest.traceState.get("fi")).toBe("r:0.5");
    expect(latest.traceState.serialize()).toBe("vendor=opaque,fi=r:0.5");
    expect(original.serialize()).toBe("vendor=opaque,fi=r:0.5");
  } finally { await Promise.all(providers.map(provider => provider.shutdown())); }
});

// SDK 2.8 admits links before attributes/onStart and calls the diagnostic logger
// synchronously on link overflow. Reenter once without depending on SDK log text.
test.each([
  { label: "no reentry", limit: 1, reenter: false, nested: "ratio", decision: SamplingDecision.RECORD_AND_SAMPLED },
  { label: "saturated ratio", limit: 1, reenter: true, nested: "ratio", decision: SamplingDecision.RECORD_AND_SAMPLED },
  { label: "spare capacity", limit: 2, reenter: true, nested: "ratio", decision: SamplingDecision.RECORD_AND_SAMPLED },
  { label: "ratio-less sampled", limit: 1, reenter: true, nested: "no-ratio", decision: SamplingDecision.RECORD_AND_SAMPLED },
  { label: "ratio-less recording", limit: 1, reenter: true, nested: "no-ratio", decision: SamplingDecision.RECORD },
  { label: "non-recording", limit: 1, reenter: true, nested: "no-ratio", decision: SamplingDecision.NOT_RECORD },
])("SDK 2.8 synchronous logger reentry: $label", async ({ limit, reenter, nested, decision }) => {
  const order: string[] = [];
  const started: ReadableSpan[] = [];
  const decisions: number[] = [];
  const delegate: Sampler = {
    shouldSample(_ctx, _traceId, name) {
      order.push(`sample:${name}`);
      const chosen = name === "nested" ? decision : SamplingDecision.RECORD_AND_SAMPLED;
      decisions.push(chosen);
      return { decision: chosen };
    },
    toString: () => "delegate",
  };
  const admission = samplingRatioAdmission({ sampler: delegate, ratio: { rate: 0.25, parentBased: true } });
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ sampler: admission.sampler,
    spanLimits: { attributeCountLimit: limit, linkCountLimit: 1 },
    spanProcessors: [{
      onStart(span) { order.push(`onStart:${span.name}`); admission.onStart(span); started.push(span); },
      onEnd() {}, async forceFlush() {}, async shutdown() {},
    }, new SimpleSpanProcessor(exporter)] });
  const tracer = provider.getTracer("test");
  const outerContext = parent("r:0.25");
  const nestedContext = parent(nested === "ratio" ? "r:0.5" : undefined);
  let nestedSpan: Span | undefined;
  let armed = false, reentered = false;
  diag.setLogger({
    error() {}, warn() {}, info() {}, verbose() {},
    debug() {
      if (!armed || !reenter || reentered) return;
      reentered = true;
      order.push("reenter");
      nestedSpan = tracer.startSpan("nested", { attributes: { existing: "nested" } }, nestedContext);
      nestedSpan.end();
    },
  }, DiagLogLevel.DEBUG);
  try {
    const attributes = Object.freeze({ existing: "outer" });
    const link: SpanContext = trace.getSpanContext(outerContext)!;
    armed = true;
    tracer.startSpan("outer", { attributes, links: [{ context: link }, { context: link }] }, outerContext).end();
    armed = false;
    await provider.forceFlush();
    expect(order).toEqual(reenter
      ? ["sample:outer", "reenter", "sample:nested", ...(decision === SamplingDecision.NOT_RECORD ? [] : ["onStart:nested"]), "onStart:outer"]
      : ["sample:outer", "onStart:outer"]);
    expect(decisions).toEqual(reenter ? [SamplingDecision.RECORD_AND_SAMPLED, decision] : [SamplingDecision.RECORD_AND_SAMPLED]);
    const outer = exporter.getFinishedSpans().find(span => span.name === "outer")!;
    expect(outer.attributes).toEqual(limit === 1 ? { [key]: 0.25 } : { existing: "outer", [key]: 0.25 });
    expect(outer.spanContext().traceState?.serialize()).toBe("fi=r:0.25");
    expect(outer.spanContext().traceId).toBe(keptId);
    expect(outer.droppedAttributesCount).toBe(limit === 1 ? 1 : 0);
    expect(attributes).toEqual({ existing: "outer" });
    if (reenter) {
      expect(nestedSpan!.spanContext().traceId).toBe(keptId);
      expect(nestedSpan!.spanContext().traceFlags).toBe(decision === SamplingDecision.RECORD_AND_SAMPLED ? TraceFlags.SAMPLED : TraceFlags.NONE);
      expect(nestedSpan!.spanContext().traceState?.serialize()).toBe(nested === "ratio" ? "fi=r:0.5" : undefined);
      const nestedStart = started.find(span => span.name === "nested");
      if (decision === SamplingDecision.NOT_RECORD) expect(nestedStart).toBeUndefined();
      else expect(nestedStart!.attributes).toEqual(nested === "ratio"
        ? (limit === 1 ? { [key]: 0.5 } : { existing: "nested", [key]: 0.5 })
        : { existing: "nested" });
    }
  } finally { armed = false; diag.disable(); await provider.shutdown(); }
});
