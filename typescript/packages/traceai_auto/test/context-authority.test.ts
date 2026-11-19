import { ok as assert } from "node:assert";
import { context, ROOT_CONTEXT, trace, type Context } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { activeAutoModelOperation, autoModelResponsibility, autoModelWireAllowed, createAutoModelOperation,
  createAutoSpanDelivery, rememberAutoModelOperation, rememberAutoSpanSink, withAutoModelOperation } from "@traceai/fi-core";
import { shouldSkip, trackCoverage } from "../src/wire/fetch";

// Authored source census, not executed counts. Scope is crossed with creation
// order, arbitrary neutral depth, explicit boundary and current native readiness.
const cases = ["before", "after"].flatMap(order => [0, 1, 4, 300].flatMap(depth =>
  ["native", "neutral", "tool", "tool-native", "sdk-tool", "sdk-tool-native"].flatMap(shape =>
    ["live", "lost", "recovered"].map(destination => ({ order, depth, shape, destination })))));
it.each(cases)("R5 context authority $order/$depth/$shape/$destination", async ({ order, depth, shape, destination }) => {
  const exporter = new InMemorySpanExporter(), sink = new SimpleSpanProcessor(exporter);
  const observer: SpanProcessor = { onStart: trackCoverage, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  rememberAutoSpanSink(observer);
  const provider = new BasicTracerProvider({ spanProcessors: [sink, observer] });
  const tracer = provider.getTracer("r5-authority");
  const members = (provider as any)._activeSpanProcessor._spanProcessors as SpanProcessor[];
  let conversionReady = false;
  const delivery = createAutoSpanDelivery(() => conversionReady);
  const operation = createAutoModelOperation(delivery);
  const carrier = trace.wrapSpanContext({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 });
  rememberAutoModelOperation(carrier, operation);
  const appKey = Symbol("application-context"), appValue = Object.freeze({ untouched: true });
  const parent = withAutoModelOperation(trace.setSpan(ROOT_CONTEXT.setValue(appKey, appValue), carrier), operation);
  const created: Array<ReturnType<typeof tracer.startSpan>> = [];
  const views: Array<{ span: any; attributes: object; snapshot: object }> = [];
  const select = (ctx: Context, kind?: string) => {
    const span = tracer.startSpan("r5-child", { attributes: kind ? { "gen_ai.span.kind": kind, app: "kept" } : { app: "kept" } }, ctx);
    created.push(span); views.push({ span, attributes: (span as any).attributes, snapshot: { ...(span as any).attributes } });
    return trace.setSpan(ctx, span);
  };
  try {
    if (order === "after") members.splice(members.indexOf(observer), 1);
    let ctx = parent;
    const boundary = shape.includes("tool");
    if (boundary) {
      // Track a genuine MODEL -> TOOL edge for the explicit-null regression.
      ctx = select(ctx, "LLM");
      ctx = select(ctx, "TOOL");
      if (!shape.startsWith("sdk-")) ctx = withAutoModelOperation(ctx);
      expect(activeAutoModelOperation(ctx)).toBeUndefined();
    }
    const ownsRequest = shape === "native" || shape.endsWith("-native");
    if (ownsRequest) ctx = select(ctx, "LLM");
    if (order === "before") members.splice(members.indexOf(observer), 1);
    for (let i = 0; i < depth; i++) ctx = select(ctx);
    // No observer remains for any of these descendants, at any tested depth.
    if (destination !== "live") members.splice(members.indexOf(sink), 1);
    if (destination === "recovered") members.push(sink);
    const covered = ownsRequest && destination !== "lost";
    const decision = autoModelResponsibility(ctx)!;
    expect(decision.scope).toBe(boundary ? "boundary" : "operation");
    expect(decision.operation).toBe(boundary ? undefined : operation);
    expect(decision.independentCoverage).toBe(covered);
    expect(shouldSkip(ctx, false)).toBe(covered);
    expect(shouldSkip(ctx)).toBe(covered);
    expect(autoModelWireAllowed(decision.operation, decision.independentCoverage, "request")).toBe(!covered);
    expect(operation.snapshot()).toEqual({ kind: "undecided" });
    expect(ctx.getValue(appKey)).toBe(appValue);
    expect(ctx.deleteValue(appKey).getValue(appKey)).toBeUndefined();
    expect(ctx.getValue(appKey)).toBe(appValue);
    expect(autoModelResponsibility(ctx.setValue(Symbol("extra"), 1))).toEqual(decision);
    expect(activeAutoModelOperation(parent)).toBe(operation);
    // A completed neutral request commits only its own operation, not a tool's
    // enclosing aggregate; readiness recovery must not rewrite that commitment.
    if (!covered && !boundary) {
      expect(operation.claimWire(() => true)).toBe(true); operation.finishWire(true);
      conversionReady = true;
      expect(operation.claimConverted()).toBe(false);
      expect(operation.snapshot()).toEqual({ kind: "wire", submitted: 1, lost: 0 });
    } else {
      conversionReady = true;
      expect(operation.claimConverted()).toBe(true);
      operation.finishConverted(true);
    }
    const next = createAutoModelOperation(delivery);
    expect(next.considerWire()).toBe(false);
    for (const view of views) {
      expect(view.span.attributes).toBe(view.attributes);
      expect(view.span.attributes).toEqual(view.snapshot);
    }
    if (!members.includes(sink)) members.push(sink);
    for (const span of [...created].reverse()) span.end();
    await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(created.length);
  } finally { await provider.shutdown(); }
});

it("R5 a scope reset is not absence, and native owners inside a boundary are still visible", async () => {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new InMemorySpanExporter())] });
  try {
    const tracer = provider.getTracer("r5-reset");
    const native = tracer.startSpan("model", { attributes: { "gen_ai.span.kind": "LLM" } });
    const parent = trace.setSpan(ROOT_CONTEXT, native);
    expect(autoModelResponsibility(parent)).toBeUndefined();
    const boundary = withAutoModelOperation(parent);
    expect(autoModelResponsibility(boundary)?.scope).toBe("boundary");
    expect(shouldSkip(boundary)).toBe(false); // Do not resurrect the selected enclosing MODEL.
    const child = tracer.startSpan("new-model", { attributes: { "fi.span.kind": "LLM" } }, boundary);
    const selected = trace.setSpan(boundary, child);
    expect(shouldSkip(selected)).toBe(true);
    expect(shouldSkip(trace.deleteSpan(selected))).toBe(false);
    expect(trace.getSpan(parent)).toBe(native);
    expect(trace.getSpan(selected)).toBe(child);
    child.end(); native.end();
  } finally { await provider.shutdown(); }
});

it("R5 late kind before descent, receiver binding and synchronous result identity are preserved", async () => {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new InMemorySpanExporter())] });
  try {
    const tracer = provider.getTracer("r5-late-kind");
    const operation = createAutoModelOperation(createAutoSpanDelivery(() => false));
    const scope = withAutoModelOperation(ROOT_CONTEXT, operation);
    const owner = tracer.startSpan("late-kind", {}, scope);
    const ownerContext = trace.setSpan(scope, owner);
    owner.setAttribute("gen_ai.span.kind", "LLM");
    const child = tracer.startSpan("neutral", {}, ownerContext);
    const leaf = trace.setSpan(ownerContext, child);
    expect(shouldSkip(leaf)).toBe(true);
    const receiver = {}, result = Promise.resolve({ kept: true });
    const invoke = function(this: unknown, value: unknown) { expect(this).toBe(receiver); expect(value).toBe(result); return result; };
    expect(context.with(leaf, invoke, receiver, result)).toBe(result);
    expect(autoModelResponsibility(leaf)?.operation).toBe(operation);
    owner.setAttribute("gen_ai.span.kind", "TOOL");
    // Candidate labels/readiness are read live; there is no cached coverage flag.
    expect(shouldSkip(leaf)).toBe(false);
    expect(activeAutoModelOperation(leaf)).toBeUndefined();
    expect(autoModelResponsibility(leaf)?.scope).toBe("boundary");
    child.end(); owner.end();
  } finally { await provider.shutdown(); }
});

const completionCases = ["absent", "undecided", "native", "converted", "wire"].flatMap(route =>
  [false, true].map(covered => ({ route, covered }))).map((row, id) => ({ ...row, id }));
it.each(completionCases)("R6-U-$id", ({ route, covered }) => {
  let conversionReady = false;
  const operation = route === "absent" ? undefined : createAutoModelOperation(createAutoSpanDelivery(() => conversionReady));
  if (route === "native") operation!.selectNative(() => true);
  if (route === "converted") { conversionReady = true; operation!.considerWire(); }
  if (route === "wire") { operation!.claimWire(() => true); operation!.finishWire(true); }
  const before = JSON.stringify(operation?.snapshot());
  const expected = !covered && route !== "native" && route !== "converted";
  assert(autoModelWireAllowed(operation, covered, "complete") === expected, "R6-U01");
  assert(JSON.stringify(operation?.snapshot()) === before, "R6-U02");
  conversionReady = true;
  // Completing an already admitted wire observation does not retroactively select
  // conversion. Neither destination recovery nor native suppression rewrites it.
  assert(autoModelWireAllowed(operation, covered, "complete") === expected, "R6-U03");
  assert(JSON.stringify(operation?.snapshot()) === before, "R6-U04");
});

it.each(["sink", "label"].map((transition, id) => ({ transition, id })))("R6-V-$id", async ({ transition }) => {
  const exporter = new InMemorySpanExporter(), sink = new SimpleSpanProcessor(exporter);
  const provider = new BasicTracerProvider({ spanProcessors: [sink] });
  const tracer = provider.getTracer("r6-completion");
  const members = (provider as any)._activeSpanProcessor._spanProcessors as SpanProcessor[];
  const operation = createAutoModelOperation(createAutoSpanDelivery(() => false));
  const parent = withAutoModelOperation(ROOT_CONTEXT, operation);
  const owner = tracer.startSpan("r6-owner", { attributes: transition === "sink" ? { "gen_ai.span.kind": "LLM" } : {} }, parent);
  const selected = trace.setSpan(parent, owner);
  const child = transition === "sink" ? tracer.startSpan("r6-neutral", {}, selected) : undefined;
  const retained = child ? trace.setSpan(selected, child) : selected;
  try {
    if (transition === "sink") members.splice(0, 1);
    const initial = autoModelResponsibility(retained)!;
    assert(!initial.independentCoverage && initial.operation === operation, "R6-V01");
    assert(autoModelWireAllowed(initial.operation, initial.independentCoverage, "request"), "R6-V02");
    if (transition === "sink") members.push(sink);
    else owner.setAttribute("gen_ai.span.kind", "LLM"); // Application labels its still-selected owner.
    const current = autoModelResponsibility(retained)!;
    assert(current.independentCoverage && current.operation === operation && owner.isRecording(), "R6-V03");
    assert(!autoModelWireAllowed(current.operation, current.independentCoverage, "complete"), "R6-V04");
    assert(operation.route === "undecided", "R6-V05");
    child?.end(); owner.end();
    await provider.forceFlush();
    assert(exporter.getFinishedSpans().filter(span => span.name === "r6-owner").length === 1, "R6-V06");
  } finally { await provider.shutdown(); }
});
