import type { AnySpan, IModelSpanTracker, SpanOutputProcessor } from "@mastra/core/observability";

/** One observer on the native stream, even when multiple FI exporters share it. */
const observed = new WeakMap<AnySpan, Set<(payload: unknown) => void>>();

/**
 * The 1.0 tracker deliberately omits tool-error chunks from exported spans. Its
 * native stream still carries the authoritative toolCallId and error together,
 * before step/root completion. Observe that lifecycle, not parsed tool inputs:
 * defaults, stripping and transforms make an argument-based join unsound.
 *
 * Installed through Mastra's native span processor/exporter-init interfaces. We
 * delegate tracker creation and streaming to Mastra, with a single pass-through
 * (no tee, second consumer, provider, spans, or changes to chunks/messages).
 */
export function toolErrorLifecycle(
  owns: (span: AnySpan) => boolean,
  capture: (span: AnySpan, payload: unknown) => void,
): SpanOutputProcessor {
  // Weak ownership: completed spans and their listeners are collectable.
  const seen = new WeakSet<AnySpan>();
  return {
    name: "future-agi-tool-error-lifecycle",
    process(span) {
      if (span?.type !== "model_generation" || !owns(span)) return span;
      // Current SDKs use direct toolCallId attributes; the floor exposes this
      // native tracker extension instead. Keep compilation compatible with both.
      const native = span as AnySpan & { createTracker?: () => IModelSpanTracker | undefined };
      if (typeof native.createTracker !== "function") return span;
      // Scope by the actual native span/instance, not a process-global agent ID.
      let listeners = observed.get(span);
      if (!listeners) {
        listeners = new Set();
        observed.set(span, listeners);
        const callbacks = listeners;
        const createTracker = native.createTracker.bind(span);
        const trackers = new WeakSet<IModelSpanTracker>();
        native.createTracker = () => {
          const tracker = createTracker();
          if (!tracker || trackers.has(tracker)) return tracker;
          trackers.add(tracker);
          const wrapStream = tracker.wrapStream.bind(tracker);
          tracker.wrapStream = stream => wrapStream(stream.pipeThrough(new TransformStream({
            transform(chunk, controller) {
              if (chunk?.type === "tool-error") {
                for (const listener of callbacks) listener(chunk.payload);
              }
              controller.enqueue(chunk);
            },
          })));
          return tracker;
        };
      }
      // A processor sees start/update/end. Register only once for this span.
      if (!seen.has(span)) {
        seen.add(span);
        listeners.add(payload => capture(span, payload));
      }
      return span;
    },
    async shutdown() {},
  };
}
