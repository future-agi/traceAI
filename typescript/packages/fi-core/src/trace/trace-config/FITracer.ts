import {
    context as apiContext,
    Attributes,
    Context,
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
  import { maskAttributes } from "./maskingRules";
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
      const fiSpan = this.startFISpan(
        this.tracer,
        name,
        opts,
        { ...contextAttributes, ...attributes },
        ctx,
      );
      // Same steps as the SDK's Tracer.startActiveSpan, but the FISpan (not the raw span) becomes
      // the active span, so code that reaches the span through the context
      // (trace.getActiveSpan()?.recordException(...), setStatus(...)) still goes through the
      // FISpan masking and error-detail policy.
      return apiContext.with(
        trace.setSpan(ctx, fiSpan),
        fn,
        undefined,
        fiSpan,
      ) as ReturnType<F>;
    }

    startSpan(name: string, options?: SpanOptions, context?: Context): FISpan {
      let tracer = this.tracer;
      if (!tracer || tracer.constructor.name === "NoopTracer") {
        diag.warn(
          `FITracer.startSpan: Internal tracer is NoopTracer or null. Returning NoopSpan for "${name}".`,
        );
        // Return an actual NoopSpan from the API to avoid downstream errors if this path is taken.
        // The global tracer can be a real one, so its span gets the masked attributes too.
        tracer = trace.getTracer("fi-noop-tracer-internal");
      }
      return this.startFISpan(tracer, name, options, options?.attributes, context);
    }

    /**
     * Creates the span and wraps it in an {@link FISpan}.
     *
     * The creation-time attributes go through the same masking as {@link FISpan.setAttributes}
     * (content hiding, error-detail hiding, PII redaction) before they reach the SDK. They are
     * still passed at creation, masked, so a sampler sees the same attributes that are exported.
     */
    private startFISpan(
      tracer: Tracer,
      name: string,
      options: SpanOptions | undefined,
      attributes: Attributes | undefined,
      context: Context | undefined,
    ): FISpan {
      const startAttributes =
        attributes != null
          ? maskAttributes({ config: this.config, attributes })
          : undefined;
      // Link attributes are span attributes too: same masking
      const links = options?.links?.map((link) =>
        link.attributes != null
          ? { ...link, attributes: maskAttributes({ config: this.config, attributes: link.attributes }) }
          : link,
      );
      const span = tracer.startSpan(
        name,
        links != null
          ? { ...options, attributes: startAttributes, links }
          : { ...options, attributes: startAttributes },
        context,
      );
      return new FISpan({ span, config: this.config, startAttributes });
    }
  }
