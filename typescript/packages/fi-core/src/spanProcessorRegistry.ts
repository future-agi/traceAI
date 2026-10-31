import { Context } from "@opentelemetry/api";
import { globalErrorHandler } from "@opentelemetry/core";
import { ReadableSpan, Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";

/** Keeps the legacy addSpanProcessor API on OTel 2's public processor interface. */
export class SpanProcessorRegistry implements SpanProcessor {
  private processors: SpanProcessor[];
  // Retain replaced processors only for flushing/shutting down already-queued spans.
  private retired: SpanProcessor[] = [];
  private shutdownPromise?: Promise<void>;

  constructor(processors: SpanProcessor[], private replaceDefault: boolean) {
    this.processors = [...processors];
  }

  add(processor: SpanProcessor): void {
    if (this.shutdownPromise) {
      throw new Error("Cannot add a span processor after shutdown");
    }
    if (this.replaceDefault) {
      this.retired.push(...this.processors);
      this.processors = [];
      this.replaceDefault = false;
    }
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
    await Promise.all([...this.retired, ...this.processors].map(async processor => {
      await processor.forceFlush();
    }));
  }

  shutdown(): Promise<void> {
    this.shutdownPromise ??= Promise.all(
      [...this.retired, ...this.processors].map(async processor => {
        await processor.shutdown();
      }),
    ).then(() => undefined);
    return this.shutdownPromise;
  }
}
