import { randomBytes } from "node:crypto";
import { context, isSpanContextValid, trace } from "@opentelemetry/api";
import type { ObservabilityBridge } from "@mastra/core/observability";

/**
 * Preserve Mastra's IDs while linking a root to the active OTel transaction.
 * The loader wraps BaseSpan's execution boundary to propagate those IDs.
 * No SDK spans are created here: the exporter alone owns span delivery.
 */
export class FIContextBridge implements ObservabilityBridge {
  name = "future-agi-context";

  createSpan(options: Parameters<ObservabilityBridge["createSpan"]>[0]): ReturnType<ObservabilityBridge["createSpan"]> {
    const active = trace.getSpanContext(context.active());
    const parent = options.parent;
    return {
      spanId: randomBytes(8).toString("hex"),
      traceId: parent?.traceId ?? options.traceId ?? (active && isSpanContextValid(active) ? active.traceId : randomBytes(16).toString("hex")),
      parentSpanId: parent?.id ?? options.parentSpanId ?? (active && isSpanContextValid(active) ? active.spanId : undefined),
    };
  }

  async exportTracingEvent(): Promise<void> {}
  async flush(): Promise<void> {}
  async shutdown(): Promise<void> {}
}
