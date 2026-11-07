import { createFetchHook } from "../src/wire/fetch-runtime";
import { activeAutoModelOperation, createAutoModelOperation, createAutoSpanDelivery, hasAutoSpanSink,
  rememberAutoModelCoverage, rememberAutoModelOperation, withAutoModelOperation, rememberAutoSpanSink, autoModelWireAllowed } from "@traceai/fi-core";
import { shouldSkip, trackCoverage } from "../src/wire/fetch";
import { ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { BasicTracerProvider, BatchSpanProcessor, NoopSpanProcessor, InMemorySpanExporter, ParentBasedSampler, SimpleSpanProcessor, TraceIdRatioBasedSampler, type ReadableSpan, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { borrowConvertedSpanDestination } from "../src/converted-spans";

it("borrows the real live SDK sinks without re-entering the attached coverage observer or owning shutdown", async () => {
  const resource = resourceFromAttributes({ "service.name": "host-service" });
  const sampler = new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(0) });
  const exporter = new InMemorySpanExporter();
  const sink = new SimpleSpanProcessor(exporter);
  const observer: SpanProcessor = { onStart: jest.fn(), onEnd: jest.fn(), forceFlush: jest.fn(async () => {}), shutdown: jest.fn(async () => {}) };
  const provider = new BasicTracerProvider({ resource, sampler, spanProcessors: [sink, observer] });
  const shutdown = jest.spyOn(sink, "shutdown"), flush = jest.spyOn(sink, "forceFlush");
  try {
    const destination = borrowConvertedSpanDestination(provider, observer)!;
    expect(destination).toBeDefined();
    expect(destination.resource).toBe(resource);
    expect(destination.sampler).toBe(sampler);
    observer.forceFlush = () => destination.processor.forceFlush();
    const late: SpanProcessor = { onStart: jest.fn(), onEnd: jest.fn(), forceFlush: jest.fn(async () => {}), shutdown: jest.fn(async () => {}) };
    (provider as any)._activeSpanProcessor._spanProcessors.push(late);
    const original = { name: "already-converted", attributes: { echo: "synthetic-borrowed-secret-not-for-FI" }, resource,
      spanContext: () => ({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 }),
    } as unknown as ReadableSpan;
    destination.processor.onStart(original as any, ROOT_CONTEXT);
    destination.processor.onEnd(original);
    await observer.forceFlush();
    expect(exporter.getFinishedSpans()).toEqual([original]);
    expect(exporter.getFinishedSpans()[0]).toBe(original);
    expect(late.onEnd).toHaveBeenCalledTimes(1);
    expect(late.onEnd).toHaveBeenCalledWith(original);
    expect(observer.onEnd).not.toHaveBeenCalled();
    expect(observer.onStart).not.toHaveBeenCalled();
    expect(flush).toHaveBeenCalledTimes(1);
    expect(late.forceFlush).toHaveBeenCalledTimes(1);
    expect(destination.isActive()).toBe(true);
    await destination.processor.shutdown();
    expect(destination.isActive()).toBe(false);
    destination.processor.onEnd(original);
    await destination.processor.forceFlush();
    expect(exporter.getFinishedSpans()).toEqual([original]);
    expect(late.onEnd).toHaveBeenCalledTimes(1);
    expect(flush).toHaveBeenCalledTimes(1);
    expect(late.forceFlush).toHaveBeenCalledTimes(1);
    expect(shutdown).not.toHaveBeenCalled();
    expect(late.shutdown).not.toHaveBeenCalled();
  } finally {
    // Only the application owner performs this final cleanup.
    await provider.shutdown();
  }
});

it("does not mistake an opaque tracer or the observer itself for a converted-span destination", () => {
  const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  const provider = { getTracer: () => ({ _spanProcessor: observer }) } as any;
  expect(borrowConvertedSpanDestination(provider, observer)).toBeUndefined();
});

it("keeps admitted forwarding active during a borrowed drain, then revokes without closing host sinks", async () => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const sink: SpanProcessor = { onStart: jest.fn(), onEnd: jest.fn(), forceFlush: jest.fn(() => pending), shutdown: jest.fn(async () => {}) };
  const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  const provider = new BasicTracerProvider({ spanProcessors: [sink, observer] });
  try {
    const destination = borrowConvertedSpanDestination(provider, observer)!;
    const flushing = destination.processor.forceFlush();
    await Promise.resolve();
    const span = { name: "already-admitted" } as ReadableSpan;
    expect(sink.forceFlush).toHaveBeenCalledTimes(1);
    expect(destination.isActive()).toBe(true);
    destination.processor.onEnd(span);
    expect(sink.onEnd).toHaveBeenCalledWith(span);
    release();
    await flushing;
    destination.revoke();
    destination.processor.onEnd(span);
    expect(sink.onEnd).toHaveBeenCalledTimes(1);
    expect(sink.shutdown).not.toHaveBeenCalled();
  } finally {
    release();
    await provider.shutdown();
  }
});

it("requires native observation in the exact live destination pipeline", async () => {
  const sink: SpanProcessor = { onStart: jest.fn(), onEnd: jest.fn(), forceFlush: jest.fn(async () => {}), shutdown: jest.fn(async () => {}) };
  const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  const provider = new BasicTracerProvider({ spanProcessors: [sink] });
  try {
    expect(borrowConvertedSpanDestination(provider, observer)).toBeUndefined();
    const members = (provider as any)._activeSpanProcessor._spanProcessors;
    members.push(observer);
    const destination = borrowConvertedSpanDestination(provider, observer)!;
    expect(destination.isActive()).toBe(true);
    members.splice(members.indexOf(observer), 1);
    expect(destination.isActive()).toBe(false);
    destination.processor.onEnd({ name: "after-detach" } as ReadableSpan);
    await destination.processor.forceFlush();
    expect(sink.onEnd).not.toHaveBeenCalled();
    expect(sink.forceFlush).not.toHaveBeenCalled();
    destination.revoke();
    members.push(observer);
    expect(destination.isActive()).toBe(false);
    expect(sink.shutdown).not.toHaveBeenCalled();
  } finally {
    await provider.shutdown();
  }
});


it("registers before synchronous entry, preserves promises/errors and releases every waiter", async () => {
  const delivery = createAutoSpanDelivery(() => true);
  let releaseOne!: () => void, releaseTwo!: () => void;
  const one = new Promise<void>(resolve => { releaseOne = resolve; });
  const two = new Promise<void>(resolve => { releaseTwo = resolve; });
  expect(delivery.admit(() => one)).toBe(one);
  expect(delivery.admit(() => two)).toBe(two);
  const expected = new Error("synthetic entry failure");
  expect(() => delivery.admit(() => { throw expected; })).toThrow(expected);
  const rejected = Promise.reject(expected);
  expect(delivery.admit(() => rejected)).toBe(rejected);
  await expect(rejected).rejects.toBe(expected);
  let settled = 0;
  const waits = [delivery.drain().then(() => settled++), delivery.drain().then(() => settled++)];
  delivery.stopAdmissions(Date.now() + 2500);
  const refused = jest.fn(async () => {});
  await delivery.admit(refused);
  expect(refused).not.toHaveBeenCalled();
  expect(delivery.isReady()).toBe(false);
  expect(delivery.canForward()).toBe(true);
  releaseOne(); await one; await Promise.resolve();
  expect(settled).toBe(0);
  releaseTwo(); await Promise.all(waits);
  expect(settled).toBe(2);
  await delivery.drain();
  delivery.revoke();
  expect(delivery.canForward()).toBe(false);
});

it("revoke releases drain waiters even when upstream never settles; deadlines never extend", async () => {
  const delivery = createAutoSpanDelivery(() => true);
  void delivery.admit(() => new Promise<void>(() => {}));
  const waiter = delivery.drain();
  delivery.stopAdmissions(Date.now() - 1);
  delivery.stopAdmissions(Date.now() + 10000);
  expect(delivery.canForward()).toBe(false);
  delivery.revoke();
  await waiter;
  await delivery.drain();
  expect(delivery.isReady()).toBe(false);
});

// One coverage/lifecycle census, reused for direct, wrapped and native-descendant
// decisions. New context entry paths must retain these live-state expectations.
it.each(["observer-removed", "sink-removed", "draining", "revoked"])(
  "synthetic coverage follows current delivery at the wire boundary: %s", async loss => {
    const sink: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
    const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
    const provider = new BasicTracerProvider({ spanProcessors: [sink, observer] });
    try {
      const destination = borrowConvertedSpanDestination(provider, observer)!;
      const carrier = trace.wrapSpanContext({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 });
      rememberAutoModelCoverage(carrier, destination.delivery.isReady);
      const parent = trace.setSpan(ROOT_CONTEXT, carrier);
      const child = trace.wrapSpanContext({ traceId: "a".repeat(32), spanId: "c".repeat(16), traceFlags: 1 });
      trackCoverage(child, parent);
      const contexts = [parent, trace.setSpan(ROOT_CONTEXT, child), trace.setSpan(ROOT_CONTEXT, { span: carrier } as any)];
      for (const ctx of contexts) expect(shouldSkip(ctx)).toBe(true);
      const members = (provider as any)._activeSpanProcessor._spanProcessors;
      if (loss === "observer-removed") members.splice(members.indexOf(observer), 1);
      if (loss === "sink-removed") members.splice(members.indexOf(sink), 1);
      if (loss === "draining") destination.delivery.stopAdmissions(Date.now() + 2500);
      if (loss === "revoked") destination.revoke();
      for (const ctx of contexts) expect(shouldSkip(ctx)).toBe(false);
      expect((carrier as any).attributes).toBeUndefined();
      const native = Object.assign(trace.wrapSpanContext(carrier.spanContext()), { attributes: { "gen_ai.span.kind": "LLM", app: "kept" } });
      rememberAutoModelCoverage(native, () => false);
      expect(shouldSkip(trace.setSpan(ROOT_CONTEXT, native))).toBe(true);
      expect(native.attributes).toEqual({ "gen_ai.span.kind": "LLM", app: "kept" });
    } finally { await provider.shutdown(); }
  },
);


// One transition census, not one readiness flag per provider/owner/loader path.
it.each(["direct", "neutral-child", "wrapped"])("operation route survives loss/recovery: %s", async shape => {
  let ready = true;
  const delivery = createAutoSpanDelivery(() => ready);
  const operation = createAutoModelOperation(delivery);
  const carrier = trace.wrapSpanContext({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 });
  rememberAutoModelOperation(carrier, operation);
  let ctx = withAutoModelOperation(trace.setSpan(ROOT_CONTEXT, carrier), operation);
  if (shape === "neutral-child") ctx = trace.setSpan(ctx, trace.wrapSpanContext({ ...carrier.spanContext(), spanId: "c".repeat(16) }));
  if (shape === "wrapped") ctx = trace.setSpan(ctx, { span: carrier } as any);
  expect(activeAutoModelOperation(ctx)).toBe(operation);
  expect(shouldSkip(ctx)).toBe(true);
  ready = false;
  expect(shouldSkip(ctx)).toBe(false);
  expect(operation.considerWire()).toBe(true);
  expect(operation.route).toBe("undecided");
  expect(operation.claimWire(() => false)).toBe(false);
  expect(operation.route).toBe("undecided");
  expect(operation.claimWire(() => true)).toBe(true);
  operation.finishWire(true);
  ready = true;
  expect(operation.route).toBe("wire");
  expect(operation.claimConverted()).toBe(false);
  expect(shouldSkip(ctx)).toBe(false); // Legitimate retry transports still capture.
  expect(operation.claimWire(() => true)).toBe(true);
  operation.finishWire(true);
  expect(operation.snapshot()).toEqual({ kind: "wire", submitted: 2, lost: 0 });
  const next = createAutoModelOperation(delivery);
  expect(next.considerWire()).toBe(false);
  expect(next.claimConverted()).toBe(true);
  next.finishConverted(true);
  expect(next.claimConverted()).toBe(false);
  expect(operation.route).toBe("wire");
  expect((carrier as any).attributes).toBeUndefined();
});

it("native context authority survives a new SDK child without onStart tracking", async () => {
  const exporter = new InMemorySpanExporter();
  const sink = new SimpleSpanProcessor(exporter);
  const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  const provider = new BasicTracerProvider({ spanProcessors: [sink, observer] });
  try {
    const destination = borrowConvertedSpanDestination(provider, observer)!;
    const native = provider.getTracer("host").startSpan("native-model", { attributes: { "gen_ai.span.kind": "LLM", app: "kept" } });
    const operation = createAutoModelOperation(destination.delivery);
    operation.selectNative(() => hasAutoSpanSink((native as any)._spanProcessor, [observer]));
    const parent = withAutoModelOperation(trace.setSpan(ROOT_CONTEXT, native), operation);
    const members = (provider as any)._activeSpanProcessor._spanProcessors;
    members.splice(members.indexOf(observer), 1);
    const child = provider.getTracer("host").startSpan("neutral", {}, parent);
    // No trackCoverage(child, parent): that observer is gone.
    expect(shouldSkip(trace.setSpan(parent, child))).toBe(true);
    expect(operation.considerWire()).toBe(false);
    expect(operation.claimConverted()).toBe(false);
    child.end(); native.end(); await provider.forceFlush();
    expect(exporter.getFinishedSpans().filter(span => span.attributes["gen_ai.span.kind"] === "LLM")).toHaveLength(1);
    expect((native as any).attributes).toEqual({ "gen_ai.span.kind": "LLM", app: "kept" });
    expect(sink).toBe(members[0]);
  } finally { await provider.shutdown(); }
});

it("scope identity isolates overlapping models, nested models and tools even with identical span IDs", () => {
  const delivery = createAutoSpanDelivery(() => true);
  const outer = createAutoModelOperation(delivery), inner = createAutoModelOperation(delivery);
  const identity = { traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 };
  const a = trace.wrapSpanContext(identity), b = trace.wrapSpanContext(identity);
  rememberAutoModelOperation(a, outer); rememberAutoModelOperation(b, inner);
  const outerContext = withAutoModelOperation(trace.setSpan(ROOT_CONTEXT, a), outer);
  const innerContext = withAutoModelOperation(trace.setSpan(outerContext, b), inner);
  expect(outer.claimWire(() => true)).toBe(true);
  outer.finishWire(true);
  expect(inner.considerWire()).toBe(false);
  expect(shouldSkip(outerContext)).toBe(false);
  expect(shouldSkip(innerContext)).toBe(true);
  expect(activeAutoModelOperation(withAutoModelOperation(innerContext))).toBeUndefined();
  expect(activeAutoModelOperation(outerContext)).toBe(outer);
});

it("irreversible conversion failure stays lost while new operations recover and drain keeps one deadline", async () => {
  let ready = true;
  const delivery = createAutoSpanDelivery(() => ready);
  const operation = createAutoModelOperation(delivery);
  expect(operation.considerWire()).toBe(false);
  let release!: () => void;
  const work = new Promise<void>(resolve => { release = resolve; });
  expect(delivery.admit(() => work)).toBe(work);
  delivery.stopAdmissions(Date.now() + 2500);
  expect(operation.claimConverted()).toBe(true);
  ready = false;
  operation.finishConverted(false);
  expect(operation.snapshot()).toEqual({ kind: "converted", stage: "lost" });
  ready = true;
  expect(operation.claimWire(() => true)).toBe(false);
  expect(operation.claimConverted()).toBe(false);
  release(); await delivery.drain();
  delivery.revoke();
  expect(delivery.canForward(() => true)).toBe(false); // Wire shares revocation too.
  expect(delivery.isReady(() => true)).toBe(false);
  const recovered = createAutoModelOperation(createAutoSpanDelivery(() => true));
  expect(recovered.claimConverted()).toBe(true);
});

it("bounds admitted formatting work and restores readiness when capacity returns", async () => {
  const delivery = createAutoSpanDelivery(() => true);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  for (let i = 0; i < 4096; i++) delivery.admit(() => held);
  const excess = jest.fn(async () => {});
  await delivery.admit(excess);
  expect(excess).not.toHaveBeenCalled();
  expect(delivery.isReady()).toBe(false);
  release(); await delivery.drain();
  expect(delivery.isReady()).toBe(true);
});

it("unsupported or unread HTTP does not claim wire, and known no-op sinks do not establish responsibility", async () => {
  class NoopSpanProcessor { onEnd() {} }
  expect(hasAutoSpanSink({ _spanProcessors: [new NoopSpanProcessor()] })).toBe(false);
  expect(hasAutoSpanSink({ _spanProcessors: [] })).toBe(false);
  let ready = false;
  const delivery = createAutoSpanDelivery(() => ready);
  const operation = createAutoModelOperation(delivery);
  const original = globalThis.fetch;
  const response = new Response("unread", { headers: { "content-type": "application/json" } });
  globalThis.fetch = jest.fn(() => Promise.resolve(response)) as any;
  const emissions = jest.fn();
  const hook = createFetchHook(65536, emissions, {
    active: () => ROOT_CONTEXT, skip: () => false, run: (_ctx, fn) => fn(),
    capture: () => operation.considerWire(),
  });
  try {
    expect(await fetch("https://example.invalid/unobserved")).toBe(response);
    expect(operation.route).toBe("undecided");
    expect(await fetch("https://api.openai.com/v1/chat/completions", { method: "POST",
      headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "mock", messages: [] }),
    })).toBe(response);
    expect(operation.route).toBe("undecided");
    expect(emissions).not.toHaveBeenCalled();
    ready = true;
    expect(operation.claimConverted()).toBe(true);
    operation.finishConverted(true);
  } finally { hook.remove(); globalThis.fetch = original; }
});


it.each(["gen_ai.span.kind", "fi.span.kind"])("closer native responsibility precedes an inherited synthetic operation: %s", async kindKey => {
  const exporter = new InMemorySpanExporter();
  const sink = new SimpleSpanProcessor(exporter);
  const provider = new BasicTracerProvider({ spanProcessors: [sink] });
  let ready = false;
  const delivery = createAutoSpanDelivery(() => ready);
  const operation = createAutoModelOperation(delivery);
  const carrier = trace.wrapSpanContext({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 });
  rememberAutoModelOperation(carrier, operation);
  const parent = withAutoModelOperation(trace.setSpan(ROOT_CONTEXT, carrier), operation);
  const native = provider.getTracer("independent-model").startSpan("closer-model", { attributes: { [kindKey]: "LLM", app: "kept" } }, parent);
  const nativeContext = trace.setSpan(parent, native);
  try {
    // Deliberately no observer/trackCoverage: creation happened after removal.
    expect(shouldSkip(nativeContext, false)).toBe(true);
    expect(shouldSkip(nativeContext)).toBe(true);
    expect(autoModelWireAllowed(operation, true, "request")).toBe(false);
    expect(operation.snapshot()).toEqual({ kind: "undecided" });
    const neutral = provider.getTracer("host").startSpan("neutral", {}, parent);
    expect(shouldSkip(trace.setSpan(parent, neutral), false)).toBe(false);
    expect(shouldSkip(trace.setSpan(parent, neutral))).toBe(false);
    // A completed separate wire request remains sticky even when a later native
    // child covers its own attempt; do not suppress future neutral retries.
    expect(operation.claimWire(() => true)).toBe(true);
    operation.finishWire(true);
    ready = true;
    expect(shouldSkip(nativeContext, false)).toBe(true);
    expect(operation.snapshot()).toEqual({ kind: "wire", submitted: 1, lost: 0 });
    expect(shouldSkip(trace.setSpan(parent, neutral))).toBe(false);
    expect(operation.claimConverted()).toBe(false);
    const next = createAutoModelOperation(delivery);
    expect(autoModelWireAllowed(next, false, "request")).toBe(false);
    expect(next.route).toBe("converted");
    expect((native as any).attributes).toEqual({ [kindKey]: "LLM", app: "kept" });
    neutral.end(); native.end(); await provider.forceFlush();
    expect(exporter.getFinishedSpans().filter(span => span.name === "closer-model")).toHaveLength(1);
  } finally { await provider.shutdown(); }
});

it("model labels without live native delivery cannot cover a synthetic operation", async () => {
  const sink = new SimpleSpanProcessor(new InMemorySpanExporter());
  const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  rememberAutoSpanSink(observer);
  const provider = new BasicTracerProvider({ spanProcessors: [sink, observer] });
  try {
    await sink.shutdown(); // Public lifecycle, not a fabricated shutdown marker.
    const operation = createAutoModelOperation(createAutoSpanDelivery(() => false));
    const carrier = trace.wrapSpanContext({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 });
    rememberAutoModelOperation(carrier, operation);
    const parent = withAutoModelOperation(trace.setSpan(ROOT_CONTEXT, carrier), operation);
    const child = provider.getTracer("host").startSpan("stopped-target", { attributes: { "gen_ai.span.kind": "LLM" } }, parent);
    expect(shouldSkip(trace.setSpan(parent, child), false)).toBe(false);
    expect(shouldSkip(trace.setSpan(parent, child))).toBe(false);
    const synthetic = Object.assign(trace.wrapSpanContext(carrier.spanContext()), { attributes: { "gen_ai.span.kind": "LLM" } });
    expect(shouldSkip(trace.setSpan(parent, synthetic), false)).toBe(false);
    child.end();
  } finally { await provider.shutdown(); }
});

it.each(["empty", "noop", "simple-stopped", "batch-stopped", "live", "opaque"])(
  "census follows our delegating wrapper to its exact supplied destination: %s", async shape => {
    const empty = new BasicTracerProvider({ spanProcessors: [] });
    const exporter = new InMemorySpanExporter();
    const supplied: SpanProcessor = shape === "empty" ? (empty as any)._activeSpanProcessor
      : shape === "noop" ? new NoopSpanProcessor()
      : shape === "batch-stopped" ? new BatchSpanProcessor(exporter)
      : shape === "opaque" ? { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} }
      : new SimpleSpanProcessor(exporter);
    const wrapper: SpanProcessor = { onStart: (span, ctx) => supplied.onStart(span, ctx), onEnd: span => supplied.onEnd(span),
      forceFlush: () => supplied.forceFlush(), shutdown: () => supplied.shutdown() };
    rememberAutoSpanSink(wrapper, supplied);
    try {
      if (shape.endsWith("-stopped")) await supplied.shutdown();
      const ready = shape === "live" || shape === "opaque";
      expect(hasAutoSpanSink(wrapper)).toBe(ready);
      const delivery = createAutoSpanDelivery(() => hasAutoSpanSink(wrapper));
      const operation = createAutoModelOperation(delivery);
      expect(autoModelWireAllowed(operation, false, "request")).toBe(!ready);
      expect(operation.route).toBe(ready ? "converted" : "undecided");
      const independent = new SimpleSpanProcessor(new InMemorySpanExporter());
      try {
        expect(hasAutoSpanSink({ _spanProcessors: [wrapper, independent] })).toBe(true);
        if (!ready) {
          expect(operation.claimWire(() => hasAutoSpanSink(independent))).toBe(true);
          operation.finishWire(true);
          expect(operation.claimConverted()).toBe(false);
          expect(operation.snapshot()).toEqual({ kind: "wire", submitted: 1, lost: 0 });
        }
      } finally { await independent.shutdown(); }
    } finally { await supplied.shutdown(); await empty.shutdown(); }
  },
);

it("wrapper census is live across membership recovery, nested wrappers and observer-only cycles", async () => {
  const empty = new BasicTracerProvider({ spanProcessors: [] });
  const target = (empty as any)._activeSpanProcessor;
  const wrapper = { onEnd() {} }, outer = { onEnd() {} };
  rememberAutoSpanSink(wrapper, target); rememberAutoSpanSink(outer, wrapper);
  const delivery = createAutoSpanDelivery(() => hasAutoSpanSink(outer));
  const operation = createAutoModelOperation(delivery);
  const sink = new SimpleSpanProcessor(new InMemorySpanExporter());
  try {
    expect(delivery.isReady()).toBe(false);
    expect(operation.claimWire(() => true)).toBe(true); operation.finishWire(true);
    target._spanProcessors.push(sink);
    expect(delivery.isReady()).toBe(true);
    expect(operation.claimConverted()).toBe(false);
    const next = createAutoModelOperation(delivery);
    expect(next.considerWire()).toBe(false);
    expect(next.claimConverted()).toBe(true);
    next.finishConverted(true);
    await sink.shutdown();
    expect(delivery.isReady()).toBe(false);
    expect(next.snapshot()).toEqual({ kind: "converted", stage: "submitted" });
    expect(createAutoModelOperation(delivery).considerWire()).toBe(true);
    rememberAutoSpanSink(wrapper, outer);
    expect(hasAutoSpanSink(outer)).toBe(false);
    rememberAutoSpanSink(wrapper);
    expect(hasAutoSpanSink(outer)).toBe(false);
  } finally { await empty.shutdown(); }
});
