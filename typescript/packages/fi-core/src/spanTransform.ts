import { ExportResult, ExportResultCode } from "@opentelemetry/core";
import { ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-base";

/** Return an export view; never mutate the SDK's ReadableSpan or its attributes. */
export type SpanTransform = (span: ReadableSpan) => ReadableSpan;

/** Compose a normalization/privacy boundary with any standard OTel exporter. */
export class TransformingSpanExporter implements SpanExporter {
  constructor(
    private readonly exporter: SpanExporter,
    private readonly transform: SpanTransform,
  ) {}

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    let transformed: ReadableSpan[];
    try {
      transformed = spans.map(this.transform);
    } catch {
      // A failed privacy transform must not fall back to the original payload.
      callback({ code: ExportResultCode.FAILED, error: new Error("Span transformation failed") });
      return;
    }
    this.exporter.export(transformed, callback);
  }

  forceFlush(): Promise<void> {
    return this.exporter.forceFlush?.() ?? Promise.resolve();
  }

  shutdown(): Promise<void> {
    return this.exporter.shutdown();
  }
}

/** Preserve SDK getters and method receivers without changing its span. */
export function spanWithAttributes(
  span: ReadableSpan,
  attributes: ReadableSpan["attributes"],
): ReadableSpan {
  const descriptor = Object.getOwnPropertyDescriptor(span, "attributes");
  if (descriptor && !descriptor.configurable && !descriptor.writable && !descriptor.set) {
    // A frozen span cannot be proxied with different attributes; copy its
    // public ReadableSpan fields instead.
    return {
      name: span.name, kind: span.kind, spanContext: () => span.spanContext(),
      parentSpanContext: span.parentSpanContext, startTime: span.startTime,
      endTime: span.endTime, status: span.status, attributes, links: span.links,
      events: span.events, duration: span.duration, ended: span.ended,
      resource: span.resource, instrumentationScope: span.instrumentationScope,
      droppedAttributesCount: span.droppedAttributesCount,
      droppedEventsCount: span.droppedEventsCount, droppedLinksCount: span.droppedLinksCount,
    };
  }
  return new Proxy(span, {
    get(target, property) {
      if (property === "attributes") return attributes;
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
