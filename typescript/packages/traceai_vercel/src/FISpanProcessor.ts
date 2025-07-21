import {
    BatchSpanProcessor,
    BufferConfig,
    ReadableSpan,
    SimpleSpanProcessor,
    Span,
    SpanExporter,
  } from "@opentelemetry/sdk-trace-base";
  import { addFIAttributesToSpan, shouldExportSpan } from "./utils";
  import { SpanFilter } from "./types";
  import { Context } from "@opentelemetry/api";
  
  /**
   * Extends {@link SimpleSpanProcessor} to support FI attributes.
   * This processor enhances spans with FI attributes before exporting them.
   * It can be configured to selectively export only FI spans or all spans.
   * @extends {SimpleSpanProcessor}
   *
   * @example
   * ```typescript
    * import { FISimpleSpanProcessor, isFISpan } from "@traceai/vercel";
   * import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto"
   *
   * const exporter = new OTLPTraceExporter();
   * const processor = new FISimpleSpanProcessor({
   *   exporter,
   *   spanFilter: isFISpan,
   * });
   * tracerProvider.addSpanProcessor(processor);
   * ```
   */
  export class FISimpleSpanProcessor extends SimpleSpanProcessor {
    private readonly spanFilter?: SpanFilter;
    constructor({
      exporter,
      spanFilter,
    }: {
      /**
       * The exporter to pass spans to.
       */
      readonly exporter: SpanExporter;
      /**
       * A filter to apply to spans before exporting. If it returns true for a given span, that span will be exported.
       */
      readonly spanFilter?: SpanFilter;
  
      config?: BufferConfig;
    }) {
      super(exporter);
      this.spanFilter = spanFilter;
    }
  
    onEnd(span: ReadableSpan): void {
      addFIAttributesToSpan(span);
      if (
        shouldExportSpan({
          span,
          spanFilter: this.spanFilter,
        })
      ) {
        super.onEnd(span);
      }
    }
  }
  
  /**
   * Extends {@link BatchSpanProcessor} to support FI attributes.
   * This processor enhances spans with FI attributes before exporting them.
   * It can be configured to selectively export only FI spans or all spans.
   * @extends {BatchSpanProcessor}
   *
   * @example
   * ```typescript
   * import { FIBatchSpanProcessor, isFISpan } from "@traceai/vercel";
   * import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto"
   *
   * const exporter = new OTLPTraceExporter();
   * const processor = new FIBatchSpanProcessor({
   *   exporter,
   *   spanFilter: isFISpan,
   *   config: { maxQueueSize: 2048, scheduledDelayMillis: 5000 },
   * });
   * tracerProvider.addSpanProcessor(processor);
   * ```
   */
  export class FIBatchSpanProcessor extends BatchSpanProcessor {
    private readonly spanFilter?: SpanFilter;
    constructor({
      exporter,
      spanFilter,
      config,
    }: {
      /**
       * The exporter to pass spans to.
       */
      readonly exporter: SpanExporter;
      /**
       * A filter to apply to spans before exporting. If it returns true for a given span, that span will be exported.
       */
      readonly spanFilter?: SpanFilter;
      /**
       * The configuration options for processor.
       */
      config?: BufferConfig;
    }) {
      super(exporter, config);
      this.spanFilter = spanFilter;
    }
  
    forceFlush(): Promise<void> {
      return super.forceFlush();
    }
  
    shutdown(): Promise<void> {
      return super.shutdown();
    }
  
    onStart(_span: Span, _parentContext: Context): void {
      return super.onStart(_span, _parentContext);
    }
  
    onEnd(span: ReadableSpan): void {
      addFIAttributesToSpan(span);
      if (
        shouldExportSpan({
          span,
          spanFilter: this.spanFilter,
        })
      ) {
        super.onEnd(span);
      }
    }
  }