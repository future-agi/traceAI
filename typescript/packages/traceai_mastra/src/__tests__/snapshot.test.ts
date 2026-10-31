import { inspect } from "node:util";
import { SpanType } from "@mastra/core/observability";
import { snapshotData, snapshotMastraSpan } from "../snapshot";
import { createFIMastraExporter } from "../FIObservability";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

function hostileDate() {
  const date = new Date("2020-01-02T03:04:05.000Z");
  const hook = jest.fn(() => { date.setUTCFullYear(2099); throw new Error("application hook ran"); });
  Object.assign(date, { toJSON: hook, getTime: hook, toISOString: hook, toString: hook, valueOf: hook });
  Object.defineProperty(date, Symbol.toPrimitive, { value: hook });
  Object.defineProperty(date, inspect.custom, { value: hook });
  return { date, hook };
}

function hostileProxy() {
  const trap = jest.fn(() => { throw new Error("application proxy trap ran"); });
  const proxy = new Proxy({}, { get: trap, getPrototypeOf: trap, getOwnPropertyDescriptor: trap, ownKeys: trap, has: trap });
  return { proxy, trap };
}

function span(values: Record<string, unknown> = {}) {
  return { id: "1".repeat(16), traceId: "2".repeat(32), name: "step", type: SpanType.WORKFLOW_STEP,
    startTime: new Date("2020-01-01T00:00:00Z"), endTime: new Date("2020-01-01T00:00:01Z"), attributes: {}, ...values };
}

describe("data-only Mastra snapshots", () => {
  it("uses intrinsic Date conversion without serialization, coercion or inspection hooks", () => {
    const { date, hook } = hostileDate();
    expect(snapshotData({ date })).toEqual({ date: "2020-01-02T03:04:05.000Z" });
    expect(Date.prototype.getUTCFullYear.call(date)).toBe(2020);
    expect(hook).not.toHaveBeenCalled();
    expect(snapshotData(new Date(NaN))).toBe("Date");
  });

  it("never invokes getters, proxy traps, custom inspect or unsupported object methods", () => {
    const { proxy, trap } = hostileProxy();
    const hook = jest.fn(() => { throw new Error("application getter ran"); });
    const revoked = Proxy.revocable({}, {});
    revoked.revoke();
    const data = Object.defineProperties({ proxy, revoked: revoked.proxy, inheritedProxy: Object.create(proxy),
      fn: hook, symbol: Symbol("do not coerce"), bigint: BigInt(7), map: new Map(), set: new Set(),
      toJSON: hook, [inspect.custom]: hook }, {
      getter: { enumerable: true, get: hook },
      [Symbol.toStringTag]: { get: hook },
      constructor: { get: hook },
    });
    const snapshot = snapshotData(data);
    expect(JSON.parse(JSON.stringify(snapshot))).toEqual({ proxy: "object", revoked: "object", inheritedProxy: "object",
      fn: "function", symbol: "symbol", bigint: "bigint", map: "object", set: "object", toJSON: "function" });
    expect(trap).not.toHaveBeenCalled();
    expect(hook).not.toHaveBeenCalled();
  });

  it("copies arrays by own data index without their iterators, getters or toJSON", () => {
    const hook = jest.fn(() => { throw new Error("application array hook ran"); });
    const array: unknown[] = [1, 2, 3];
    Object.defineProperty(array, "1", { get: hook });
    Object.defineProperty(array, "toJSON", { get: hook });
    Object.defineProperty(array, Symbol.iterator, { get: hook });
    expect(JSON.stringify(snapshotData(array))).toBe('[1,"undefined",3]');
    expect(hook).not.toHaveBeenCalled();
  });

  it("bounds cycles, deep/branching graphs, sparse arrays and non-finite values", () => {
    const cycle: any = { value: 1 }; cycle.self = cycle;
    expect(snapshotData(cycle)).toEqual({ value: 1, self: "object" });
    let deep: any = {};
    for (let i = 0; i < 1000; i++) deep = { deep };
    expect(JSON.stringify(snapshotData(deep)).length).toBeLessThan(500);
    let branch: any = { value: 1 };
    for (let i = 0; i < 20; i++) branch = { left: branch, right: branch };
    expect(JSON.stringify(snapshotData(branch)).length).toBeLessThan(500000);
    expect(snapshotData(new Array(2 ** 32 - 1))).toBe("object");
    expect(snapshotData([NaN, Infinity, -Infinity])).toEqual(["NaN", "Infinity", "-Infinity"]);
  });

  it("detaches span timing using intrinsic getTime and preserves millisecond precision", () => {
    const { date, hook } = hostileDate();
    Date.prototype.setUTCMilliseconds.call(date, 123);
    const snapshot = snapshotMastraSpan(span({ startTime: date, endTime: date }));
    expect(snapshot.startTime).not.toBe(date);
    expect(snapshot.startTime.getTime()).toBe(Date.prototype.getTime.call(date));
    expect(snapshot.startTime.getTime() % 1000).toBe(123);
    expect(hook).not.toHaveBeenCalled();
  });

  it("preserves model completion timing and invocation aliases using detached native dates", async () => {
    const { date, hook } = hostileDate();
    const spans: ReadableSpan[] = [];
    const exporter = createFIMastraExporter({ apiKey: "test-key", secretKey: "test-secret",
      spanProcessor: { onStart() {}, onEnd(converted) { spans.push(converted); }, async forceFlush() {}, async shutdown() {} } });
    const original = span({ type: SpanType.MODEL_GENERATION, attributes: { model: "mock", provider: "mock", completionStartTime: date,
      parameters: { topK: 7, stopSequences: ["visible-stop"] } } });
    await (exporter as any)._exportTracingEvent({ type: "span_ended", exportedSpan: original });
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["mastra.completion_start_time"]).toBe("2020-01-02T03:04:05.000Z");
    expect(JSON.parse(spans[0].attributes["gen_ai.request.parameters"] as string)).toEqual({ top_k: 7, stop_sequences: '["visible-stop"]' });
    expect(hook).not.toHaveBeenCalled();
    await exporter.shutdown();
  });

  it("snapshots before asynchronous conversion and rejects container hooks without calling them", async () => {
    const { proxy, trap } = hostileProxy();
    const spans: ReadableSpan[] = [];
    const exporter = createFIMastraExporter({ apiKey: "test-key", secretKey: "test-secret",
      spanProcessor: { onStart() {}, onEnd(converted) { spans.push(converted); }, async forceFlush() {}, async shutdown() {} } });
    const date = new Date("2020-01-01T00:00:00Z");
    const output = { date, text: "before" };
    const pending = (exporter as any)._exportTracingEvent({ type: "span_ended", exportedSpan: span({ output }) });
    date.setUTCFullYear(2021); output.text = "after";
    await pending;
    await (exporter as any)._exportTracingEvent(proxy);
    await (exporter as any)._exportTracingEvent({ type: "span_ended", exportedSpan: proxy });
    await (exporter as any)._exportTracingEvent(Object.defineProperty({ type: "span_ended" }, "exportedSpan", { get: trap }));
    expect(spans).toHaveLength(1);
    expect(JSON.parse(spans[0].attributes["output.value"] as string)).toEqual({ date: "2020-01-01T00:00:00.000Z", text: "before" });
    expect(trap).not.toHaveBeenCalled();
    await exporter.shutdown();
  });

  it("protects every converter surface, not just workflow input/output", async () => {
    const { date, hook } = hostileDate();
    const { proxy, trap } = hostileProxy();
    const getter = jest.fn(() => { throw new Error("application metadata getter ran"); });
    const input = Object.defineProperty({ date, proxy }, "getter", { enumerable: true, get: getter });
    const spans: ReadableSpan[] = [];
    const exporter = createFIMastraExporter({ apiKey: "test-key", secretKey: "test-secret",
      spanProcessor: { onStart() {}, onEnd(converted) { spans.push(converted); }, async forceFlush() {}, async shutdown() {} } });
    const original = span({ startTime: date, endTime: date, input, output: { date },
      metadata: { date, input }, tags: [date, proxy], isRootSpan: true,
      attributes: { availableTools: input }, errorInfo: { id: "Error", message: "unchanged", details: { stack: input } } });
    await (exporter as any)._exportTracingEvent({ type: "span_ended", exportedSpan: original });
    expect(spans).toHaveLength(1);
    const converted = spans[0];
    expect(JSON.parse(converted.attributes["input.value"] as string)).toEqual({ date: "2020-01-02T03:04:05.000Z", proxy: "object" });
    expect(converted.attributes["mastra.metadata.date"]).toBe("2020-01-02T03:04:05.000Z");
    expect(converted.attributes["mastra.tags"]).toBe('["2020-01-02T03:04:05.000Z","object"]');
    expect(converted.startTime).toEqual([1577934245, 0]);
    expect(converted.duration).toEqual([0, 0]);
    expect((original as any).output.date).toBe(date);
    expect(hook).not.toHaveBeenCalled();
    expect(trap).not.toHaveBeenCalled();
    expect(getter).not.toHaveBeenCalled();
    await exporter.shutdown();
  });
});
