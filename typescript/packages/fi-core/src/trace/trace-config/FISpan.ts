import {
    Span,
    SpanContext,
    SpanStatus,
    SpanStatusCode,
    TimeInput,
    Attributes,
    Exception,
    AttributeValue,
    Link,
    diag,
  } from "@opentelemetry/api";
  import { TraceConfig } from "./types";
  import { mask } from "./maskingRules";
  import {
    ERROR_TYPE,
    ERROR_TYPE_OTHER,
    EXCEPTION_EVENT_NAME,
    EXCEPTION_TYPE,
    getErrorType,
    getHiddenExceptionEventAttributes,
    isErrorDetailAttribute,
    sanitizeExceptionEventAttributes,
    sanitizeStatus,
    stripErrorDetailAttributes,
  } from "./errorDetails";
  
  /**
   * True when the value is a {@link TimeInput} (HrTime, epoch millis or Date) rather than {@link Attributes}
   */
  function isTimeInput(value: unknown): value is TimeInput {
    return (
      typeof value === "number" ||
      value instanceof Date ||
      (Array.isArray(value) &&
        value.length === 2 &&
        typeof value[0] === "number" &&
        typeof value[1] === "number")
    );
  }
  
  /**
   * A wrapper around the OpenTelemetry {@link Span} interface that masks sensitive information based on the passed in {@link TraceConfig}.
   *
   * It is also the single place where the error-detail policy is enforced (see `errorDetails.ts`),
   * for {@link recordException}, {@link setStatus}, {@link addEvent} and {@link setAttribute(s)}:
   * - always: an ERROR status description is the exception message only, never a stack trace, and a
   *   span that ends in ERROR gets `error.type`;
   * - with `hideErrorDetails` on: the ERROR status has no description, exception events keep only
   *   `exception.type` (and `exception.escaped`), and `exception.message`, `exception.stacktrace`,
   *   `error.message` (and `*.error.message`-style keys) never reach the underlying span.
   */
  export class FISpan implements Span {
    private readonly span: Span;
    private readonly config: TraceConfig;
    private isEnded: boolean = false;
    /** The instrumentation set `error.type` itself; keep its value */
    private hasExplicitErrorType: boolean = false;
    /** `error.type` derived from the last recorded exception */
    private recordedErrorType?: string;
    /** The last exception recorded on the span, used to reduce "Name: message" descriptions */
    private lastException?: Exception;
    /** The last status set on the span is ERROR */
    private hasErrorStatus: boolean = false;
  
    constructor({ span, config }: { span: Span; config: TraceConfig }) {
      this.span = span;
      this.config = config;
      const wrappedSpanContext = span.spanContext();
      const isNoOp = wrappedSpanContext.traceFlags === 0 && wrappedSpanContext.spanId === '0000000000000000';
      // diag.debug(
      //     `FISpan CONSTRUCTOR: Wrapping span. ID: ${wrappedSpanContext.spanId}, TraceID: ${wrappedSpanContext.traceId}, TraceFlags: ${wrappedSpanContext.traceFlags}, Is NoOp: ${isNoOp}`
      // );
    }
  
    /**
     * Returns the value to write for the attribute, or undefined when it must be dropped
     */
    private maskAttribute(key: string, value?: AttributeValue): AttributeValue | undefined {
      if (this.config.hideErrorDetails && isErrorDetailAttribute(key)) {
        return undefined;
      }
      if (key === ERROR_TYPE && value != null) {
        this.hasExplicitErrorType = true;
      }
      return mask({ config: this.config, key, value });
    }
  
    setAttribute(key: string, value: AttributeValue): this {
      if (this.isEnded) {
        diag.warn(`FISpan.setAttribute called on ended span ID: ${this.span.spanContext().spanId} for key: ${key}`);
        return this;
      }
      const maskedValue = this.maskAttribute(key, value);
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
          const maskedValue = this.maskAttribute(key, value);
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
      // addEvent(name, startTime) carries no attributes
      const [attributes, time] = isTimeInput(attributesOrStartTime)
        ? [undefined, attributesOrStartTime]
        : [attributesOrStartTime, startTime];
      if (name === EXCEPTION_EVENT_NAME) {
        const exceptionType = attributes?.[EXCEPTION_TYPE];
        if (typeof exceptionType === "string" && exceptionType.length > 0) {
          this.recordedErrorType = exceptionType;
        }
      }
      if (!this.config.hideErrorDetails) {
        this.span.addEvent(name, attributesOrStartTime, startTime);
        return this;
      }
      this.span.addEvent(
        name,
        name === EXCEPTION_EVENT_NAME
          ? // still record the (type-only) event
            sanitizeExceptionEventAttributes(attributes ?? {}, true)
          : stripErrorDetailAttributes(attributes),
        time,
      );
      return this;
    }
  
    setStatus(status: SpanStatus): this {
      if (this.isEnded) return this;
      // The SDK keeps the last status set, so the last one decides whether error.type is added
      this.hasErrorStatus = status.code === SpanStatusCode.ERROR;
      this.span.setStatus(
        sanitizeStatus(status, this.config.hideErrorDetails, this.lastException),
      );
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
      if (this.hasErrorStatus && !this.hasExplicitErrorType) {
        this.span.setAttribute(ERROR_TYPE, this.recordedErrorType ?? ERROR_TYPE_OTHER);
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
      this.recordedErrorType = getErrorType(exception);
      this.lastException = exception;
      if (this.config.hideErrorDetails) {
        this.span.addEvent(
          EXCEPTION_EVENT_NAME,
          getHiddenExceptionEventAttributes(exception),
          time,
        );
        return;
      }
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