import { Span, SpanContext, SpanStatus, TimeInput, Attributes, Exception, AttributeValue, Link } from "@opentelemetry/api";
import { TraceConfig } from "./types";
/**
 * A wrapper around the OpenTelemetry {@link Span} interface that masks sensitive information based on the passed in {@link TraceConfig}.
 */
export declare class FISpan implements Span {
    private readonly span;
    private readonly config;
    private isEnded;
    constructor({ span, config }: {
        span: Span;
        config: TraceConfig;
    });
    setAttribute(key: string, value: AttributeValue): this;
    setAttributes(attributes: Attributes): this;
    addEvent(name: string, attributesOrStartTime?: Attributes | TimeInput, startTime?: TimeInput): this;
    setStatus(status: SpanStatus): this;
    updateName(name: string): this;
    end(endTime?: TimeInput): void;
    isRecording(): boolean;
    recordException(exception: Exception, time?: TimeInput): void;
    spanContext(): SpanContext;
    addLink(link: Link): this;
    addLinks(links: Link[]): this;
}
