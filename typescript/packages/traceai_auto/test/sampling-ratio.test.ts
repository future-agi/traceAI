import { createTraceState, ROOT_CONTEXT, SpanKind, trace, TraceFlags, type Context } from "@opentelemetry/api";
import { ParentBasedSampler, SamplingDecision, TraceIdRatioBasedSampler, type Sampler, type SamplingResult } from "@opentelemetry/sdk-trace-base";
import { withSamplingRatio } from "../src/sampling-ratio";
import { validTracestate } from "../src/outgoing-w3c";

const keptId = "00000000000000000000000000000001";
const droppedId = "ffffffff000000000000000000000000";
const key = "fi.sampling.ratio";
const parent = (value?: string, sampled = true, remote = true, traceId = keptId) => trace.setSpanContext(ROOT_CONTEXT, {
  traceId, spanId: "1234567890123456", traceFlags: sampled ? TraceFlags.SAMPLED : TraceFlags.NONE,
  isRemote: remote, traceState: createTraceState(value === undefined ? "vendor=opaque" : `vendor=opaque,fi=${value}`),
});
const sample = (sampler: Sampler, ctx: Context = ROOT_CONTEXT, id = keptId) =>
  sampler.shouldSample(ctx, id, "test", SpanKind.INTERNAL, { input: "attribute" }, []);
const ratioSampler = (parentBased = true, rate = 0.25) => {
  const root = new TraceIdRatioBasedSampler(rate);
  return withSamplingRatio({ sampler: parentBased ? new ParentBasedSampler({ root }) : root, ratio: { rate, parentBased } });
};

test("a deterministic kept root writes its ratio and its local child inherits it", () => {
  const sampler = ratioSampler();
  const root = sample(sampler);
  expect(root.decision).toBe(SamplingDecision.RECORD_AND_SAMPLED);
  expect(root.attributes?.[key]).toBe(0.25);
  expect(root.traceState?.get("fi")).toBe("r:0.25");
  const child = sample(sampler, parent(root.traceState!.get("fi"), true, false));
  expect(child.attributes?.[key]).toBe(0.25);
  expect(child.traceState?.get("fi")).toBe("r:0.25");
});

describe.each([0, 0.25, 1])("parent-based local root rate %s", rate => {
  test.each([true, false])("a valid sampled parent supplies the trace-wide ratio (remote=%s)", remote => {
    const result = sample(ratioSampler(true, rate), parent("r:0.5", true, remote));
    expect(result.decision).toBe(SamplingDecision.RECORD_AND_SAMPLED);
    expect(result.attributes?.[key]).toBe(0.5);
    expect(result.traceState?.get("fi")).toBe("r:0.5");
    expect(result.traceState?.get("vendor")).toBe("opaque");
  });

  test.each([true, false])("unsampled parents are not stamped (remote=%s)", remote => {
    const result = sample(ratioSampler(true, rate), parent("r:0.5", false, remote));
    expect(result.decision).toBe(SamplingDecision.NOT_RECORD);
    expect(result.attributes?.[key]).toBeUndefined();
  });

  test.each([undefined, "r:2", "r:abc", "r:NaN", "r:Infinity", "r:0", "r:-0.1", "r:1", "0.5", "r:", "r:0x0.8", "r: 0.5", "r:1e309", "r:1e-999"])("missing or invalid parent fi=%s does not invent a ratio", value => {
    const result = sample(ratioSampler(true, rate), parent(value));
    expect(result.decision).toBe(SamplingDecision.RECORD_AND_SAMPLED);
    expect(result.attributes?.[key]).toBeUndefined();
  });
});

describe.each([true, false])("invalid-parent root metadata (parentBased=%s)", parentBased => {
  test.each(["traceId", "spanId"] as const)("ignores stale state from a zero %s", field => {
    const original = createTraceState("vendor=stale-old-trace,fi=r:0.5");
    const ctx = trace.setSpanContext(ROOT_CONTEXT, {
      traceId: keptId, spanId: "1234567890123456", traceFlags: TraceFlags.SAMPLED,
      [field]: "0".repeat(field === "traceId" ? 32 : 16), traceState: original,
    });
    const root = new TraceIdRatioBasedSampler(0.25);
    const delegate = parentBased ? new ParentBasedSampler({ root }) : root;
    const result = sample(withSamplingRatio({ sampler: delegate, ratio: { rate: 0.25, parentBased } }), ctx);
    expect(result.decision).toBe(sample(delegate, ctx).decision);
    expect(result.attributes?.[key]).toBe(0.25);
    expect(result.traceState?.serialize()).toBe("fi=r:0.25");
    expect(result.traceState?.get("vendor")).toBeUndefined();
    expect(original.serialize()).toBe("vendor=stale-old-trace,fi=r:0.5");
  });

  test("preserves explicit delegate state even with an invalid parent", () => {
    const original = createTraceState("delegate=retained,fi=r:0.75");
    const delegate: Sampler = { shouldSample: () => ({ decision: SamplingDecision.RECORD_AND_SAMPLED, traceState: original }), toString: () => "delegate" };
    const result = sample(withSamplingRatio({ sampler: delegate, ratio: { rate: 0.25, parentBased } }), parent("r:0.5", true, true, "0".repeat(32)));
    expect(result.traceState?.serialize()).toBe("fi=r:0.25,delegate=retained");
    expect(original.serialize()).toBe("delegate=retained,fi=r:0.75");
  });
});

test.each([true, false])("plain ratio uses the local ratio and replaces fi even with a sampled=%s parent", sampled => {
  const result = sample(ratioSampler(false), parent("r:0.5", sampled));
  expect(result.attributes?.[key]).toBe(0.25);
  expect(result.traceState?.get("fi")).toBe("r:0.25");
  expect(result.traceState?.get("vendor")).toBe("opaque");
});

test.each([
  { state: Array.from({ length: 32 }, (_, i) => `v${i}=opaque`).join(","), retained: "v0", removed: "v31", count: 32 },
  { state: `a=${"x".repeat(256)},b=${"y".repeat(251)}`, retained: "a", removed: "b", count: 2 },
  { state: `fi=r:0.5,a=${"x".repeat(256)},b=${"y".repeat(242)}`, retained: "a", removed: "b", count: 2 },
])("local ratio keeps tracestate within W3C limits ($removed evicted)", ({ state, retained, removed, count }) => {
  expect(validTracestate(state)).toBe(true);
  const original = createTraceState(state);
  const ctx = trace.setSpanContext(parent(), { ...trace.getSpanContext(parent())!, traceState: original });
  const result = sample(ratioSampler(false), ctx);
  expect(result.attributes?.[key]).toBe(0.25);
  expect(result.traceState?.get("fi")).toBe("r:0.25");
  expect(validTracestate(result.traceState!.serialize())).toBe(true);
  expect(result.traceState!.serialize().split(",")).toHaveLength(count);
  expect(result.traceState?.get(retained)).toBe(original.get(retained));
  expect(result.traceState?.get(removed)).toBeUndefined();
  expect(original.get(removed)).toBeDefined();
});

test.each([0.1, 0.12345678901234568, 1e-7, Number.MIN_VALUE])("ratio %s uses a shortest round-trippable representation", rate => {
  const sampler: Sampler = { shouldSample: () => ({ decision: SamplingDecision.RECORD_AND_SAMPLED }), toString: () => "kept" };
  const result = sample(withSamplingRatio({ sampler, ratio: { rate, parentBased: false } }));
  expect(result.traceState?.get("fi")).toBe(`r:${String(rate)}`);
  expect(Number(result.traceState!.get("fi")!.slice(2))).toBe(rate);
});

test.each([-0.1, 2, NaN, Infinity, -Infinity])("invalid local ratio %s returns the original sampler and never inherits metadata", rate => {
  const sampler = new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(1) });
  const wrapped = withSamplingRatio({ sampler, ratio: { rate, parentBased: true } });
  expect(wrapped).toBe(sampler);
  expect(sample(wrapped, parent("r:0.5")).attributes?.[key]).toBeUndefined();
});

test.each([0, 1])("parent-based boundary %s does not stamp local roots", rate => {
  const result = sample(ratioSampler(true, rate));
  expect(result.decision).toBe(rate === 1 ? SamplingDecision.RECORD_AND_SAMPLED : SamplingDecision.NOT_RECORD);
  expect(result.attributes?.[key]).toBeUndefined();
  expect(result.traceState?.get("fi")).toBeUndefined();
});

test.each([0, 1])("non-parent-based boundary %s remains unwrapped and never inherits", rate => {
  const sampler = new TraceIdRatioBasedSampler(rate);
  const wrapped = withSamplingRatio({ sampler, ratio: { rate, parentBased: false } });
  expect(wrapped).toBe(sampler);
  expect(sample(wrapped, parent("r:0.5")).attributes?.[key]).toBeUndefined();
});

test.each(["r:0.12345678901234568", "r:1e-7", `r:${Number.MIN_VALUE}`])("full-rate parent-based sampling preserves valid parent entry %s verbatim", entry => {
  const result = sample(ratioSampler(true, 1), parent(entry));
  expect(result.attributes?.[key]).toBe(Number(entry.slice(2)));
  expect(result.traceState?.get("fi")).toBe(entry);
});

describe.each([0, 0.25, 1])("parent-based delegate preservation at root rate %s", rate => {
  test.each([SamplingDecision.NOT_RECORD, SamplingDecision.RECORD])("does not stamp or change decision %s under a sampled parent", decision => {
    const result: SamplingResult = { decision, attributes: { original: "value" }, traceState: createTraceState("vendor=opaque,fi=r:0.5") };
    const sampler: Sampler = { shouldSample: () => result, toString: () => "delegate" };
    const wrapped = withSamplingRatio({ sampler, ratio: { rate, parentBased: true } });
    expect(sample(wrapped, parent("r:0.5"))).toBe(result);
  });

  test("never stamps an unsampled parent even if a custom delegate samples it", () => {
    const result: SamplingResult = { decision: SamplingDecision.RECORD_AND_SAMPLED };
    const sampler: Sampler = { shouldSample: () => result, toString: () => "delegate" };
    const wrapped = withSamplingRatio({ sampler, ratio: { rate, parentBased: true } });
    expect(sample(wrapped, parent("r:0.5", false))).toBe(result);
  });
});

test("an unknown custom sampler is not inspected or wrapped", () => {
  const sampler: Sampler = { shouldSample: jest.fn(() => ({ decision: SamplingDecision.RECORD_AND_SAMPLED })), toString: () => "TraceIdRatioBased{0.25}" };
  expect(withSamplingRatio({ sampler })).toBe(sampler);
});

test.each([SamplingDecision.NOT_RECORD, SamplingDecision.RECORD, SamplingDecision.RECORD_AND_SAMPLED])("delegates all inputs and preserves result fields for decision %s", decision => {
  const traceState = createTraceState("other=result,fi=r:0.75");
  const result: SamplingResult = { decision, attributes: { original: "value" }, traceState };
  const delegate: Sampler = { shouldSample: jest.fn(() => result), toString: () => "delegate" };
  const wrapper = withSamplingRatio({ sampler: delegate, ratio: { rate: 0.25, parentBased: false } });
  const ctx = parent("r:0.5"), attributes = { original: "input" }, links = [{ context: trace.getSpanContext(ctx)! }];
  const actual = wrapper.shouldSample(ctx, keptId, "name", SpanKind.CLIENT, attributes, links);
  expect(delegate.shouldSample).toHaveBeenCalledTimes(1);
  expect(delegate.shouldSample).toHaveBeenCalledWith(ctx, keptId, "name", SpanKind.CLIENT, attributes, links);
  expect(actual.decision).toBe(decision);
  expect(actual.attributes?.original).toBe("value");
  expect(actual.traceState?.get("other")).toBe("result");
  expect(result.attributes).toEqual({ original: "value" });
  expect(traceState.get("fi")).toBe("r:0.75");
  if (decision === SamplingDecision.RECORD_AND_SAMPLED) {
    expect(actual.attributes?.[key]).toBe(0.25);
    expect(actual.traceState?.get("fi")).toBe("r:0.25");
  } else {
    expect(actual).toBe(result);
    expect(actual.attributes?.[key]).toBeUndefined();
  }
});

test.each([true, false])("wrapper decisions match the SDK across parent and trace-id inputs (parentBased=%s)", parentBased => {
  const root = new TraceIdRatioBasedSampler(0.25);
  const delegate = parentBased ? new ParentBasedSampler({ root }) : root;
  const wrapper = withSamplingRatio({ sampler: delegate, ratio: { rate: 0.25, parentBased } });
  for (const ctx of [ROOT_CONTEXT, parent("r:0.5"), parent(undefined), parent("r:0.5", false), parent("r:0.5", true, false)]) {
    for (const id of [keptId, droppedId, "1".repeat(32), "abcdef0123456789abcdef0123456789"]) {
      expect(sample(wrapper, ctx, id).decision).toBe(sample(delegate, ctx, id).decision);
      if (sample(delegate, ctx, id).decision !== SamplingDecision.RECORD_AND_SAMPLED) {
        expect(sample(wrapper, ctx, id).attributes?.[key]).toBeUndefined();
      }
    }
  }
});
