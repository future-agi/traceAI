import { globalErrorHandler } from "@opentelemetry/core";
import { BatchSpanProcessor, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import type { SpanExporter } from "@opentelemetry/sdk-trace-base";
import type { Context } from "@opentelemetry/api";
import type { ReadableSpan, Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { rememberAutoSpanSink } from "./auto-span-delivery";

// Compatibility defaults stay real SDK processors at the top level, so native
// adapters can borrow the transport while shutdown still drains accepted work.
export class DefaultSimpleSpanProcessor extends SimpleSpanProcessor {
  private enabled = true;
  disable(): void { this.enabled = false; rememberAutoSpanSink(this); }
  onEnd(span: ReadableSpan): void { if (this.enabled) super.onEnd(span); }
}
export class DefaultBatchSpanProcessor extends BatchSpanProcessor {
  private enabled = true;
  constructor(private readonly destination: SpanExporter) { super(destination); }
  disable(): void { this.enabled = false; rememberAutoSpanSink(this); }
  onEnd(span: ReadableSpan): void { if (this.enabled) super.onEnd(span); }
  async forceFlush(): Promise<void> {
    await super.forceFlush();
    await this.destination.forceFlush?.();
  }
}
function reportHookFailure(): void {
  // Preserve the SDK error-handler notification, never application error text.
  try { globalErrorHandler(new Error("Span processor hook failed")); } catch { /* Preserve sibling hooks. */ }
}

/** Keeps the legacy addSpanProcessor API on OTel 2's public processor interface. */
export class SpanProcessorRegistry implements SpanProcessor {
  private readonly processors: SpanProcessor[];
  private shutdownPromise?: Promise<void>;

  constructor(processors: SpanProcessor[], private readonly disableDefault?: () => void) {
    this.processors = [...processors];
    // Trusted core-owned composition, not an opaque onEnd claim. The census reads
    // this same live list on every request, including through retained Contexts.
    rememberAutoSpanSink(this, { _spanProcessors: this.processors });
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
      } catch { reportHookFailure(); }
    }
  }

  // Recent OTel 2.x SDKs call this optional hook; keep compatibility with the
  // 2.0.1 type surface while forwarding it when the installed SDK supports it.
  onEnding(span: Span): void {
    if (this.shutdownPromise) return;
    for (const processor of this.processors) {
      try {
        (processor as SpanProcessor & { onEnding?(span: Span): void }).onEnding?.(span);
      } catch { reportHookFailure(); }
    }
  }

  onEnd(span: ReadableSpan): void {
    if (this.shutdownPromise) return;
    for (const processor of this.processors) {
      try {
        processor.onEnd(span);
      } catch { reportHookFailure(); }
    }
  }

  async forceFlush(): Promise<void> {
    await Promise.all(this.processors.map(async processor => {
      await processor.forceFlush();
    }));
  }

  shutdown(): Promise<void> {
    if (!this.shutdownPromise) {
      // Stop acceptance synchronously; retained children still own their drain.
      rememberAutoSpanSink(this);
      // Publish the promise before calling application code, including reentrant
      // shutdown/add attempts. Async mapping also isolates synchronous throws.
      this.shutdownPromise = Promise.resolve().then(() => Promise.all(
        this.processors.map(async processor => {
          await processor.shutdown();
        }),
      )).then(() => undefined);
    }
    return this.shutdownPromise;
  }
}
