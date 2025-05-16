import {
    Span,
    SpanContext,
    SpanStatus,
    TimeInput,
    Attributes,
    Exception,
    AttributeValue,
    Link,
    diag,
  } from "@opentelemetry/api";
  import { TraceConfig } from "./types";
  import { mask } from "./maskingRules";
  
  /**
   * A wrapper around the OpenTelemetry {@link Span} interface that masks sensitive information based on the passed in {@link TraceConfig}.
   */
  export class FISpan implements Span {
    private readonly span: Span;
    private readonly config: TraceConfig;
    private isEnded: boolean = false;
  
    constructor({ span, config }: { span: Span; config: TraceConfig }) {
      this.span = span;
      this.config = config;
      const wrappedSpanContext = span.spanContext();
      const isNoOp = wrappedSpanContext.traceFlags === 0 && wrappedSpanContext.spanId === '0000000000000000';
      // diag.debug(
      //     `FISpan CONSTRUCTOR: Wrapping span. ID: ${wrappedSpanContext.spanId}, TraceID: ${wrappedSpanContext.traceId}, TraceFlags: ${wrappedSpanContext.traceFlags}, Is NoOp: ${isNoOp}`
      // );
    }
  
    setAttribute(key: string, value: AttributeValue): this {
      if (this.isEnded) {
        diag.warn(`FISpan.setAttribute called on ended span ID: ${this.span.spanContext().spanId} for key: ${key}`);
        return this;
      }
      const maskedValue = mask({ config: this.config, key, value });
      if (maskedValue != null) {
        this.span.setAttribute(key, maskedValue);
      }
      return this;
    }
  
    setAttributes(attributes: Attributes): this {
      if (this.isEnded) {
        diag.warn(`FISpan.setAttributes called on ended span ID: ${this.span.spanContext().spanId}`);
        return this;
      }
      const maskedAttributes = Object.entries(attributes).reduce(
        (acc, [key, value]) => {
          const maskedValue = mask({ config: this.config, key, value });
          if (maskedValue != null) {
            acc[key] = maskedValue;
          }
          return acc;
        },
        {} as Attributes,
      );
      this.span.setAttributes(maskedAttributes);
      return this;
    }
  
    addEvent(
      name: string,
      attributesOrStartTime?: Attributes | TimeInput,
      startTime?: TimeInput,
    ): this {
      if (this.isEnded) return this;
      this.span.addEvent(name, attributesOrStartTime, startTime);
      return this;
    }
  
    setStatus(status: SpanStatus): this {
      if (this.isEnded) return this;
      this.span.setStatus(status);
      return this;
    }
  
    updateName(name: string): this {
      if (this.isEnded) return this;
      this.span.updateName(name);
      return this;
    }
  
    end(endTime?: TimeInput): void {
      if (this.isEnded) {
        diag.warn(`FISpan.end called on already ended span ID: ${this.span.spanContext().spanId}`);
        return;
      }
      this.span.end(endTime);
      this.isEnded = true;
      // diag.debug(
      //     `FISpan.end CALLED for span ID: ${this.span.spanContext().spanId}`
      // );
    }
  
    isRecording(): boolean {
      return this.span.isRecording();
    }
  
    recordException(exception: Exception, time?: TimeInput): void {
      if (this.isEnded) return;
      this.span.recordException(exception, time);
    }
  
    spanContext(): SpanContext {
      return this.span.spanContext();
    }
  
    addLink(link: Link): this {
      if (this.isEnded) return this;
      this.span.addLink(link);
      return this;
    }
  
    addLinks(links: Link[]): this {
      if (this.isEnded) return this;
      this.span.addLinks(links);
      return this;
    }
  }