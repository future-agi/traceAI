import {
    context as apiContext,
    context,
    Context,
    Span,
    SpanOptions,
    Tracer,
    diag,
    trace,
  } from "@opentelemetry/api";
  import {
    FIActiveSpanCallback,
    TraceConfig,
    TraceConfigOptions,
  } from "./types";
  import { FISpan } from "./FISpan";
  import { generateTraceConfig } from "./traceConfig";
  import { getAttributesFromContext } from "../contextAttributes";
  
  /**
   * Formats the params for the startActiveSpan method
   * The method has multiple overloads, so we need to format the arguments
   * Taken from @see https://github.com/open-telemetry/opentelemetry-js/blob/main/packages/opentelemetry-sdk-trace-base/src/Tracer.ts#L220C3-L235C6
   *
   */
  function formatStartActiveSpanParams<F extends FIActiveSpanCallback>(
    arg2?: SpanOptions | F,
    arg3?: Context | F,
    arg4?: F,
  ) {
    let opts: SpanOptions | undefined;
    let ctx: Context | undefined;
    let fn: F;
  
    if (typeof arg2 === "function") {
      fn = arg2;
    } else if (typeof arg3 === "function") {
      opts = arg2;
      fn = arg3;
    } else {
      opts = arg2;
      ctx = arg3;
      fn = arg4 as F;
    }
  
    opts = opts ?? {};
    ctx = ctx ?? apiContext.active();
  
    return { opts, ctx, fn };
  }
  
  /**
   * A wrapper around the OpenTelemetry {@link Tracer} interface that masks sensitive information based on the passed in {@link TraceConfig}.
   */
  export class FITracer implements Tracer {
    private readonly tracer: Tracer;
    private readonly config: TraceConfig;
    /**
     *
     * @param tracer The OpenTelemetry {@link Tracer} to wrap
     * @param traceConfig The {@link TraceConfigOptions} to set to control the behavior of the tracer
     */
    constructor({
      tracer,
      traceConfig,
    }: {
      tracer: Tracer;
      traceConfig?: TraceConfigOptions;
    }) {
      this.tracer = tracer;
      this.config = generateTraceConfig(traceConfig);
      // ADDED LOG
      // diag.debug(
      //   `FITracer CONSTRUCTOR: Received tracer type: ${tracer?.constructor?.name}. TraceConfig provided: ${!!traceConfig}`
      // );
    }
    startActiveSpan<F extends (span: FISpan) => unknown>(
      name: string,
      fn: F,
    ): ReturnType<F>;
    startActiveSpan<F extends (span: FISpan) => unknown>(
      name: string,
      options: SpanOptions,
      fn: F,
    ): ReturnType<F>;
    startActiveSpan<F extends (span: FISpan) => unknown>(
      name: string,
      options: SpanOptions,
      context: Context,
      fn: F,
    ): ReturnType<F>;
    startActiveSpan<F extends (span: FISpan) => ReturnType<F>>(
      name: string,
      arg2?: F | SpanOptions,
      arg3?: F | Context,
      arg4?: F,
    ): ReturnType<F> | undefined {
      const formattedArgs = formatStartActiveSpanParams(arg2, arg3, arg4);
      if (formattedArgs == null) {
        return;
      }
      const { opts, ctx, fn } = formattedArgs;
      const { attributes } = opts ?? {};
      const contextAttributes = getAttributesFromContext(ctx);
      const mergedAttributes = { ...contextAttributes, ...attributes };
      return this.tracer.startActiveSpan(
        name,
        { ...opts, attributes: undefined },
        ctx,
        (span: Span) => {
          const fiSpan = new FISpan({
            span,
            config: this.config,
          });
          fiSpan.setAttributes(mergedAttributes);
          return fn(fiSpan);
        },
      );
    }
  
    startSpan(name: string, options?: SpanOptions, context?: Context): FISpan {
      // ADDED LOG
        // diag.debug(
        //     `FITracer.startSpan CALLED for name: "${name}". Internal this.tracer type: ${this.tracer?.constructor?.name}`
        // );

      if (!this.tracer || this.tracer.constructor.name === "NoopTracer") {
        diag.warn(
          `FITracer.startSpan: Internal tracer is NoopTracer or null. Returning NoopSpan for "${name}".`
        );
        // Return an actual NoopSpan from the API to avoid downstream errors if this path is taken.
        const noopOtelSpan = trace.getTracer("fi-noop-tracer-internal").startSpan(name, options, context);
        return new FISpan({ span: noopOtelSpan, config: this.config });
      }
      
      const { attributes: originalAttributes, ...otherOptions } = options ?? {};

      const span = this.tracer.startSpan(
        name,
        { ...otherOptions, attributes: originalAttributes },
        context,
      );

      // ADDED LOGS - CRITICAL
      const spanContext = span.spanContext();
      const isNoOp = spanContext.traceFlags === 0 && spanContext.spanId === '0000000000000000';
      // diag.debug(
      //   `FITracer.startSpan: OTel tracer (${this.tracer?.constructor?.name}) created span. ID: ${spanContext.spanId}, TraceID: ${spanContext.traceId}, TraceFlags: ${spanContext.traceFlags}, Is NoOp: ${isNoOp}`
      // );
      
      return new FISpan({ span, config: this.config });
    }
  }