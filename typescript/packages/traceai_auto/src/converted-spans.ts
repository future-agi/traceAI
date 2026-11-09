import { createAutoSpanDelivery, hasAutoSpanSink, type AutoSpanDelivery } from "@traceai/fi-core";
import type { TracerProvider } from "@opentelemetry/api";
import type { ReadableSpan, Sampler, SpanProcessor } from "@opentelemetry/sdk-trace-base";

/** @internal A borrowed SDK destination for frameworks that already own IDs. */
export interface ConvertedSpanDestination {
  processor: SpanProcessor;
  resource: ReadableSpan["resource"];
  sampler: Sampler;
  delivery: AutoSpanDelivery;
  isActive(): boolean;
  revoke(): void;
}

/**
 * SDK 2.x has no public API for submitting an already-created ReadableSpan. Use
 * the selected tracer's live processor list (the same SDK seam as attach), not
 * a new span/provider, which would replace Mastra IDs, timing and parents.
 * The coverage observer is deliberately NOT this forwarding adapter: attaching
 * the adapter to the host would re-enter the pipeline recursively.
 */
export function borrowConvertedSpanDestination(provider: TracerProvider, observer: SpanProcessor): ConvertedSpanDestination | undefined {
  const tracer = provider.getTracer("@traceai/mastra") as any;
  const pipeline = tracer._spanProcessor;
  const resource = tracer._resource;
  const sampler = tracer._sampler as Sampler | undefined;
  if (!Array.isArray(pipeline?._spanProcessors) || !pipeline._spanProcessors.includes(observer) || !resource || typeof sampler?.shouldSample !== "function") return undefined;
  // One contract owns both upstream work and live destination readiness. An empty
  // host pipeline is not coverage; no sink/provider identity is cached as ready.
  const delivery = createAutoSpanDelivery(() => Array.isArray(pipeline._spanProcessors)
    && pipeline._spanProcessors.includes(observer)
    && hasAutoSpanSink(pipeline, [observer, processor]));
  const isActive = delivery.canForward;
  const revoke = delivery.revoke;
  const members = (): SpanProcessor[] => isActive()
    ? pipeline._spanProcessors.filter((item: SpanProcessor) => item !== observer && item !== processor)
    : [];
  const processor: SpanProcessor = {
    onStart() {}, // Conversion is not a second native SDK span creation.
    onEnd(span) {
      for (const member of members()) {
        if (!isActive()) break;
        if (!pipeline._spanProcessors.includes(member)) continue;
        try { member.onEnd(span); } catch { /* A failed host sink must not affect Mastra or other sinks. */ }
      }
    },
    async forceFlush() {
      await Promise.all(members().map(member => Promise.resolve().then(() => {
        if (members().includes(member)) return member.forceFlush();
      })));
    },
    async shutdown() { revoke(); }, // Revoke our forwarding, never shut down borrowed sinks.
  };
  return { processor, resource, sampler, delivery, isActive, revoke };
}
