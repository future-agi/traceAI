import { Context } from "@opentelemetry/api";
import { globalErrorHandler } from "@opentelemetry/core";
import { BatchSpanProcessor, SimpleSpanProcessor, ReadableSpan, Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";

// OTel 2 fixes the processor list at construction. Stop new deliveries when the
// legacy add API replaces the default, but keep its SDK-owned flush/shutdown.
// Keep actual SDK processors at the top level: released native adapters (such as
// VoltAgent) borrow the default exporter's transport for their own batch queue.
export class DefaultSimpleSpanProcessor extends SimpleSpanProcessor {
  private enabled = true;
  disable(): void { this.enabled = false; }
  onEnd(span: ReadableSpan): void {
    if (this.enabled) super.onEnd(span);
  }
}

export class DefaultBatchSpanProcessor extends BatchSpanProcessor {
  private enabled = true;
  disable(): void { this.enabled = false; }
  onEnd(span: ReadableSpan): void {
    if (this.enabled) super.onEnd(span);
  }
}

/** Keeps the legacy addSpanProcessor API on OTel 2's public processor interface. */
export class SpanProcessorRegistry implements SpanProcessor {
  private readonly processors: SpanProcessor[];
  private shutdownPromise?: Promise<void>;

  constructor(processors: SpanProcessor[], private readonly disableDefault?: () => void) {
    this.processors = [...processors];
  }

  add(processor: SpanProcessor): void {
    if (this.shutdownPromise) {
      throw new Error("Cannot add a span processor after shutdown");
    }
    this.disableDefault?.();
    this.processors.push(processor);
  }

  onStart(span: Span, parentContext: Context): void {
    if (this.shutdownPromise) return;
    for (const processor of this.processors) {
      try {
        processor.onStart(span, parentContext);
      } catch (error) {
        globalErrorHandler(error as Error);
      }
    }
  }

  // Recent OTel 2.x SDKs call this optional hook; keep compatibility with the
  // 2.0.1 type surface while forwarding it when the installed SDK supports it.
  onEnding(span: Span): void {
    if (this.shutdownPromise) return;
    for (const processor of this.processors) {
      try {
        (processor as SpanProcessor & { onEnding?(span: Span): void }).onEnding?.(span);
      } catch (error) {
        globalErrorHandler(error as Error);
      }
    }
  }

  onEnd(span: ReadableSpan): void {
    if (this.shutdownPromise) return;
    for (const processor of this.processors) {
      try {
        processor.onEnd(span);
      } catch (error) {
        globalErrorHandler(error as Error);
      }
    }
  }

  async forceFlush(): Promise<void> {
    await Promise.all(this.processors.map(async processor => {
      await processor.forceFlush();
    }));
  }

  shutdown(): Promise<void> {
    this.shutdownPromise ??= Promise.all(
      this.processors.map(async processor => {
        await processor.shutdown();
      }),
    ).then(() => undefined);
    return this.shutdownPromise;
  }
}
