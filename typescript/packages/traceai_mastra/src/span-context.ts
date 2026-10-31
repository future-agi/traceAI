import { context, isSpanContextValid, ROOT_CONTEXT, trace, TraceFlags } from "@opentelemetry/api";
import type { Context, Span, SpanContext } from "@opentelemetry/api";
import { SamplingDecision } from "@opentelemetry/sdk-trace-base";
import type { SpanProcessor, TraceIdRatioBasedSampler } from "@opentelemetry/sdk-trace-base";

interface Metadata {
  context: SpanContext;
  /** The agent already receives this exact identity from the native SDK. */
  native: boolean;
}
interface State {
  live: WeakMap<object, Metadata>;
  exported: WeakMap<object, Metadata>;
  processors: WeakSet<object>;
}
const KEY = Symbol.for("traceai.mastra.span-context.v1");
const CREATION = Symbol.for("traceai.mastra.span-creation.v1");
const globals = globalThis as any;
const state: State = globals[KEY] ?? (globals[KEY] = {
  live: new WeakMap(), exported: new WeakMap(), processors: new WeakSet(),
});

/** Share decisions across CJS/ESM copies without retaining completed trace IDs. */
export function exportedSpanMetadata(span: object): Metadata | undefined {
  return state.exported.get(span);
}

export function samplingContext(
  span: { traceId: string; id: string; parent?: object },
  sampler: TraceIdRatioBasedSampler,
  upstream: Context = context.active(),
): SpanContext {
  const parent = (span.parent && state.live.get(span.parent)?.context) ?? trace.getSpanContext(upstream);
  return {
    traceId: span.traceId,
    spanId: span.id,
    traceFlags: parent && isSpanContextValid(parent) && parent.traceId === span.traceId
      ? parent.traceFlags
      : sampler.shouldSample(ROOT_CONTEXT, span.traceId).decision === SamplingDecision.NOT_RECORD
        ? TraceFlags.NONE : TraceFlags.SAMPLED,
  };
}

/** Observe only native spans created synchronously by Mastra's bridge factory. */
export function observeNativeSpans(processor: SpanProcessor): void {
  if (state.processors.has(processor)) return;
  const original = processor.onStart;
  processor.onStart = function(span, parent) {
    original.call(this, span, parent);
    try { (context.active().getValue(CREATION) as Span[] | undefined)?.push(span); }
    catch { /* Observation must never affect the SDK. */ }
  };
  state.processors.add(processor);
}

export function createWithContext<T>(create: () => T, sampler: TraceIdRatioBasedSampler): T {
  const upstream = context.active();
  const native: Span[] = [];
  const result = context.with(upstream.setValue(CREATION, native), create);
  try {
    const span = result as any;
    if (span?.isValid === false || !isSpanContextValid({ traceId: span.traceId, spanId: span.id, traceFlags: 0 })) return result;
    const own = native.find(item => {
      const candidate = item.spanContext();
      return candidate.traceId === span.traceId && candidate.spanId === span.id;
    });
    const identity = own?.spanContext();
    state.live.set(span, {
      context: identity ? { traceId: identity.traceId, spanId: identity.spanId, traceFlags: identity.traceFlags } : samplingContext(span, sampler, upstream),
      native: !!own,
    });
  } catch { /* Unsupported custom spans keep their original behavior. */ }
  return result;
}

export function liveSpanContext(span: any, sampler: TraceIdRatioBasedSampler): SpanContext | undefined {
  if (span.isValid === false) return undefined;
  const identity = state.live.get(span)?.context ?? samplingContext(span, sampler);
  return isSpanContextValid(identity) ? identity : undefined;
}

export function rememberExport(span: object, snapshot: object): void {
  const metadata = state.live.get(span);
  if (metadata) state.exported.set(snapshot, metadata);
}
