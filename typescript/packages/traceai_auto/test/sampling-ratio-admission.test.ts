import { ROOT_CONTEXT, trace, TraceFlags, createTraceState } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, TraceIdRatioBasedSampler, ParentBasedSampler, SamplingDecision } from "@opentelemetry/sdk-trace-base";
import { FITracer, privacyFilter } from "@traceai/fi-core";
import { samplingRatioAdmission } from "../src/sampling-ratio";

const key = "fi.sampling.ratio";
const keptId = "00000000000000000000000000000001";
const parent = (entry?: string) => trace.setSpanContext(ROOT_CONTEXT, {
  traceId: keptId, spanId: "1234567890123456", traceFlags: TraceFlags.SAMPLED,
  traceState: entry === undefined ? undefined : createTraceState(`fi=${entry}`),
});

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
