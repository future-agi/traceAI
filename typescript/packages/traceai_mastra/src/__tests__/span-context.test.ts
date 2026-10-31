import { context, createContextKey, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import type { SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { TraceIdRatioBasedSampler } from "@opentelemetry/sdk-trace-base";
import { createWithContext, exportedSpanMetadata, observeNativeSpans, rememberExport } from "../span-context";

const identity = { traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 };

describe("Mastra identity-only context metadata", () => {
  afterEach(() => jest.restoreAllMocks());

  it("observes native starts idempotently without rewriting the original parent context", () => {
    const onStart = jest.fn();
    const processor = { onStart } as unknown as SpanProcessor;
    const legacy = createContextKey("traceai.auto.library.credentials");
    const parent = ROOT_CONTEXT.setValue(legacy, ["synthetic-legacy-credential"]);
    const span = {} as Parameters<SpanProcessor["onStart"]>[0];
    observeNativeSpans(processor);
    const wrapped = processor.onStart;
    observeNativeSpans(processor);
    expect(processor.onStart).toBe(wrapped);
    processor.onStart(span, parent);
    expect(onStart).toHaveBeenCalledTimes(1);
    expect(onStart.mock.instances[0]).toBe(processor);
    expect(onStart.mock.calls[0]).toEqual([span, parent]);
    expect(onStart.mock.calls[0][1]).toBe(parent);
  });

  it("keeps only identity and native dedup decisions on exported snapshots", () => {
    jest.spyOn(context, "active").mockReturnValue(trace.setSpanContext(ROOT_CONTEXT, identity));
    const span = { traceId: identity.traceId, id: identity.spanId };
    expect(createWithContext(() => span, new TraceIdRatioBasedSampler(0))).toBe(span);
    const snapshot = {};
    rememberExport(span, snapshot);
    expect(exportedSpanMetadata(snapshot)).toEqual({ context: identity, native: false });
    expect(Object.keys(exportedSpanMetadata(snapshot)!).sort()).toEqual(["context", "native"]);
  });

  it("does not override an unsampled upstream parent with root sampling", () => {
    jest.spyOn(context, "active").mockReturnValue(trace.setSpanContext(ROOT_CONTEXT, { ...identity, traceFlags: 0 }));
    const span = { traceId: identity.traceId, id: identity.spanId };
    createWithContext(() => span, new TraceIdRatioBasedSampler(1));
    const snapshot = {};
    rememberExport(span, snapshot);
    expect(exportedSpanMetadata(snapshot)?.context.traceFlags).toBe(0);
  });

  it("preserves the original factory error", () => {
    const error = new Error("original factory error");
    expect(() => createWithContext(() => { throw error; }, new TraceIdRatioBasedSampler(1))).toThrow(error);
  });

  it("leaves invalid custom spans untouched and without an export association", () => {
    const span = { isValid: false };
    expect(createWithContext(() => span, new TraceIdRatioBasedSampler(1))).toBe(span);
    const snapshot = {};
    rememberExport(span, snapshot);
    expect(exportedSpanMetadata(snapshot)).toBeUndefined();
  });
});
