import { Context, SpanOptions, Tracer } from "@opentelemetry/api";
import { TraceConfigOptions } from "./types";
import { FISpan } from "./FISpan";
/**
 * A wrapper around the OpenTelemetry {@link Tracer} interface that masks sensitive information based on the passed in {@link TraceConfig}.
 */
export declare class FITracer implements Tracer {
    private readonly tracer;
    private readonly config;
    /**
     *
     * @param tracer The OpenTelemetry {@link Tracer} to wrap
     * @param traceConfig The {@link TraceConfigOptions} to set to control the behavior of the tracer
     */
    constructor({ tracer, traceConfig, }: {
        tracer: Tracer;
        traceConfig?: TraceConfigOptions;
    });
    startActiveSpan<F extends (span: FISpan) => unknown>(name: string, fn: F): ReturnType<F>;
    startActiveSpan<F extends (span: FISpan) => unknown>(name: string, options: SpanOptions, fn: F): ReturnType<F>;
    startActiveSpan<F extends (span: FISpan) => unknown>(name: string, options: SpanOptions, context: Context, fn: F): ReturnType<F>;
    startSpan(name: string, options?: SpanOptions, context?: Context): FISpan;
}
