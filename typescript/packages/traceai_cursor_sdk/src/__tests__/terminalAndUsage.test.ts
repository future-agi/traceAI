import { ROOT_CONTEXT, SpanStatusCode } from "@opentelemetry/api";
import { CursorAttributes as C, SpanNames, StandardAttributes as S } from "../attributes";
import { RunTracer } from "../spans";
import { RunLike, SDKMessageLike, TokenUsageLike } from "../types";
import { RunWatcher, traceStream } from "../wrappers";
import { drain, memoryProvider, one } from "./helpers";

const { provider, exporter } = memoryProvider();
const registry = new Set<RunWatcher>();
const policy = { hideInputs: true, hideOutputs: true };

function tracer() {
  return new RunTracer({ tracer: provider.getTracer("regression"), policy, parentContext: ROOT_CONTEXT });
}

function watch(run: RunLike, graceMs = 10) {
  return new RunWatcher(run, tracer(), { tracer: provider.getTracer("regression"), policy, registry, graceMs, drainMs: 20 });
}

beforeEach(() => {
  exporter.reset();
  jest.useFakeTimers();
});

afterEach(() => {
  for (const watcher of registry) watcher.flush();
  jest.useRealTimers();
});

afterAll(() => provider.shutdown());

describe("initial terminal status", () => {
  it.each(["finished", "error", "cancelled"])("automatically ends an already-%s run and removes its listener", (status) => {
    const dispose = jest.fn();
    const watcher = watch({ status, onDidChangeStatus: () => dispose });
    expect(watcher.inGrace).toBe(true);
    expect(registry.size).toBe(1);
    jest.advanceTimersByTime(9);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    jest.advanceTimersByTime(1);
    const span = one(exporter.getFinishedSpans(), SpanNames.RUN);
    expect(span.attributes[C.RUN_STATUS]).toBe(status);
    expect(span.status.code).toBe(status === "finished" ? SpanStatusCode.OK : SpanStatusCode.ERROR);
    expect(registry.size).toBe(0);
    expect(dispose).toHaveBeenCalledTimes(1);
    expect(watcher.inGrace).toBe(false);
  });

  it.each([undefined, () => { throw new Error("no listener support"); }])("handles a terminal run even if subscription is unavailable (%p)", (onDidChangeStatus) => {
    watch({ status: "finished", onDidChangeStatus });
    jest.advanceTimersByTime(10);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(registry.size).toBe(0);
  });

  it("checks the snapshot after subscribing, including a transition during attachment", () => {
    let status = "running";
    const dispose = jest.fn();
    watch({
      get status() { return status; },
      onDidChangeStatus() { status = "finished"; return dispose; },
    });
    jest.advanceTimersByTime(10);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(dispose).toHaveBeenCalledTimes(1);
  });

  it("does not double-schedule or restart grace for inline and duplicate notifications", () => {
    let notify!: (status: string) => void;
    const dispose = jest.fn();
    const watcher = watch({
      status: "finished",
      onDidChangeStatus(listener) { notify = listener; listener("finished"); return dispose; },
    });
    expect(jest.getTimerCount()).toBe(1);
    jest.advanceTimersByTime(9);
    notify("finished");
    jest.advanceTimersByTime(1);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    notify("finished");
    expect(dispose).toHaveBeenCalledTimes(1);
    expect(watcher.inGrace).toBe(false);
    expect(registry.size).toBe(0);
  });

  it("still permits buffered tool events to be consumed during grace", async () => {
    const watcher = watch({ status: "finished" });
    jest.advanceTimersByTime(9);
    const events: SDKMessageLike[] = [
      { type: "tool_call", call_id: "read-1", name: "read", status: "running" },
      { type: "tool_call", call_id: "read-1", name: "read", status: "completed" },
    ];
    const source = (async function* () { yield* events; })();
    expect(await drain(traceStream(source, watcher))).toEqual(events);
    const spans = exporter.getFinishedSpans();
    const root = one(spans, SpanNames.RUN);
    const tool = one(spans, `${SpanNames.TOOL_PREFIX}read`);
    expect(tool.parentSpanContext?.spanId).toBe(root.spanContext().spanId);
    expect(tool.attributes[C.TOOL_INCOMPLETE]).toBeUndefined();
    expect(registry.size).toBe(0);
    expect(watcher.inGrace).toBe(false);
  });

  it("bounds an abandoned buffered stream by the drain timeout", async () => {
    const watcher = watch({ status: "finished" });
    const source = (async function* () {
      yield { type: "tool_call", call_id: "read-1", name: "read", status: "running" };
    })();
    const stream = traceStream(source, watcher);
    await stream.next();
    jest.advanceTimersByTime(19);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    jest.advanceTimersByTime(1);
    expect(one(exporter.getFinishedSpans(), SpanNames.RUN).attributes[C.RUN_STREAM_DETACHED]).toBe(true);
    expect(one(exporter.getFinishedSpans(), `${SpanNames.TOOL_PREFIX}read`).attributes[C.TOOL_INCOMPLETE]).toBe(true);
    expect(registry.size).toBe(0);
    await stream.return();
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("defers even zero-grace completion until the caller can start reading", () => {
    watch({ status: "finished" }, 0);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    jest.advanceTimersByTime(0);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("does not close a running run until a terminal notification arrives", () => {
    let notify!: (status: string) => void;
    let status = "running";
    watch({ get status() { return status; }, onDidChangeStatus(listener) { notify = listener; return () => undefined; } });
    jest.advanceTimersByTime(1000);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    status = "finished";
    notify(status);
    jest.advanceTimersByTime(10);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
});

describe.each(["run", "events"] as const)("normalized %s token usage", (source) => {
  function attributes(usage: TokenUsageLike) {
    const runTracer = tracer();
    if (source === "events") runTracer.onEvent({ type: "usage", usage });
    runTracer.finish({ kind: "result", result: { status: "finished", ...(source === "run" ? { usage } : {}) } });
    return one(exporter.getFinishedSpans(), SpanNames.RUN).attributes;
  }

  it.each([20, 15, undefined])("normalizes complete cache-inclusive counts regardless of raw total %p", (totalTokens) => {
    const a = attributes({ inputTokens: 10, outputTokens: 5, cacheReadTokens: 3, cacheWriteTokens: 2, reasoningTokens: 4, totalTokens });
    expect(a[S.INPUT_TOKENS]).toBe(15);
    expect(a[S.OUTPUT_TOKENS]).toBe(5);
    expect(a[S.TOTAL_TOKENS]).toBe(20);
    expect(a[C.USAGE_TOTAL_TOKENS]).toBe(totalTokens);
    expect(a[C.USAGE_INPUT_TOKENS]).toBe(10);
    expect(a[S.REASONING_TOKENS]).toBe(4); // Already included in output, not added again.
    expect(a[C.USAGE_SOURCE]).toBe(source);
  });

  it.each<TokenUsageLike>([
    { outputTokens: 5, cacheReadTokens: 3, totalTokens: 99 },
    { inputTokens: 10, cacheWriteTokens: 2, totalTokens: 99 },
    { totalTokens: 99 },
    { inputTokens: Number.NaN, outputTokens: 5, totalTokens: 99 },
  ])("preserves raw total when input or output is incomplete (%p)", (usage) => {
    const a = attributes(usage);
    expect(a[S.TOTAL_TOKENS]).toBe(99);
    expect(a[C.USAGE_TOTAL_TOKENS]).toBe(99);
  });

  it("does not invent a total from partial usage without a reported total", () => {
    const a = attributes({ inputTokens: 10, cacheReadTokens: 3 });
    expect(a[S.INPUT_TOKENS]).toBe(13);
    expect(a[S.TOTAL_TOKENS]).toBeUndefined();
    expect(a[C.USAGE_TOTAL_TOKENS]).toBeUndefined();
  });

  it("treats zero input and output as complete, including cache-only input", () => {
    const a = attributes({ inputTokens: 0, outputTokens: 0, cacheReadTokens: 3, totalTokens: 0 });
    expect(a[S.INPUT_TOKENS]).toBe(3);
    expect(a[S.TOTAL_TOKENS]).toBe(3);
    expect(a[C.USAGE_TOTAL_TOKENS]).toBe(0);
  });
});
