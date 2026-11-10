import {
    context as apiContext,
    Attributes,
    Context,
    SpanOptions,
    Tracer,
    TracerProvider,
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
  import { getAttributesFromContext, getSession } from "../contextAttributes";
  import { type PrivacyHints, rememberSessionIdentity } from "../../privacy-runtime";
  import { privacyFilter, rememberAuthoredRedactions, rememberProjectedAttributeDrops } from "../../export-privacy";
  import { projectAttributes, tracerAttributeLimit } from "../../attribute-projection";
  
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
  
  // Only tracers created by the auto-only facade select destination mode. A
  // shared native tracer is never marked, so explicit standalone FITracer users
  // on the same provider keep their wrapper semantics. Share identities across
  // installed CJS/ESM core copies; this stores no telemetry or capture records.
  const AUTO_TRACERS = Symbol.for("traceai.auto.destination.tracers.v1");
  type AutoTracers = { version: 1; native: WeakMap<object, Tracer> };
  function autoTracers(): AutoTracers {
    const shared = globalThis as typeof globalThis & { [AUTO_TRACERS]?: AutoTracers };
    const existing = shared[AUTO_TRACERS];
    if (existing) {
      if (existing.version !== 1 || !(existing.native instanceof WeakMap)) throw new Error("Unsupported auto destination protocol");
      return existing;
    }
    const registry: AutoTracers = Object.freeze({ version: 1, native: new WeakMap<object, Tracer>() });
    Object.defineProperty(shared, AUTO_TRACERS, { value: registry });
    return registry;
  }

  /**
   * A wrapper around the OpenTelemetry {@link Tracer} interface that masks sensitive information based on the passed in {@link TraceConfig}.
   */
  export class FITracer implements Tracer {
    private readonly tracer: Tracer;
    private readonly config: TraceConfig;
    private readonly autoDestination: boolean;

    /** @internal Pass only to auto instrumentors, never install globally.
     * SDK pipeline/resource slots remain visible to mapping adapters. Public SDK
     * calls are bound to their original receiver; no native object is patched. */
    static autoProvider(provider: TracerProvider): TracerProvider {
      const facades = new WeakMap<object, Tracer>();
      const facade = Object.create(provider) as TracerProvider;
      Object.defineProperty(facade, "getTracer", { value: (...args: Parameters<TracerProvider["getTracer"]>) => {
        const native = provider.getTracer(...args);
        let wrapped = facades.get(native);
        if (!wrapped) {
          wrapped = Object.create(native) as Tracer;
          Object.defineProperties(wrapped, {
            startSpan: { value: native.startSpan.bind(native) },
            startActiveSpan: { value: native.startActiveSpan.bind(native) },
          });
          autoTracers().native.set(wrapped, native);
          facades.set(native, wrapped);
        }
        return wrapped;
      } });
      return facade;
    }
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
      const native = autoTracers().native.get(tracer);
      this.autoDestination = native !== undefined;
      this.tracer = native ?? tracer;
      const resolved = generateTraceConfig(traceConfig);
      // Image hiding is independent of error-detail hiding. Error values still
      // cross the media/cap/PII admission and detached export boundaries.
      this.config = resolved;
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
        getSession(ctx)?.sessionId,
      );
      // Auto application writes use the native handle; only the instrumentor's
      // explicit callback handle applies FI write-time policy. Standalone keeps
      // its existing active-wrapper contract.
      return apiContext.with(
        trace.setSpan(ctx, fiSpan.getContextSpan()),
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
      sessionId?: string,
    ): FISpan {
      // Exact-secret/media/hide sanitization precedes SDK count/value limits.
      // Otherwise truncation can turn a known credential into an unknown prefix.
      let startAttributes: Attributes | undefined;
      let startPrivacyHints: PrivacyHints = new Map(), startAdmissionFailed = false;
      // Auto-only accounting input: the caller's non-null start view, pre-admission.
      let authoredStart: Attributes = {};
      try {
        const privacy = privacyFilter(this.config);
        if (this.autoDestination) startPrivacyHints = privacy.admissionHints(attributes);
        if (attributes != null) authoredStart = Object.fromEntries(Object.entries(attributes).filter(([, value]) => value != null));
        startAttributes = attributes != null ? privacy.admissionAttributes(authoredStart, undefined, this.autoDestination ? startPrivacyHints : undefined) : undefined;
      } catch { startAttributes = {}; startAdmissionFailed = true; }
      const attributeCountLimit = tracerAttributeLimit(tracer);
      // In auto mode the host alone owns native count admission. This interim
      // does not recover attributes dropped before the detached export boundary.
      const projected = this.autoDestination
        ? { attributes: startAttributes ?? {}, dropped: 0 }
        : projectAttributes(startAttributes ?? {}, attributeCountLimit);
      // Link attributes are span attributes too: same masking
      const links = options?.links?.map((link) => {
        if (link.attributes == null) return link;
        try { return { ...link, attributes: privacyFilter(this.config).admissionAttributes(link.attributes) }; }
        catch { return { ...link, attributes: {} }; }
      });
      const span = tracer.startSpan(
        name,
        links != null
          ? { ...options, attributes: startAttributes === undefined ? undefined : projected.attributes, links }
          : { ...options, attributes: startAttributes === undefined ? undefined : projected.attributes },
        context,
      );
      // Auto only: bind the NUMBER of start values FI withheld or changed to the
      // export lineage. Nothing is written to, or replayed from, native storage.
      if (this.autoDestination) {
        try { rememberAuthoredRedactions(span.spanContext(), authoredStart, startAttributes ?? {}); }
        catch { /* Accounting never changes application behavior. */ }
      }
      // Only a typed context session, unchanged by caller overrides and privacy
      // admission, may retain its semantic identity at the FI export boundary.
      // A truncated SDK value will not match this full-value binding at export.
      if (typeof sessionId === "string" && startAttributes?.["session.id"] === sessionId) rememberSessionIdentity(span.spanContext(), sessionId);
      if (projected.dropped && typeof (span as any)._droppedAttributesCount === "number") {
        (span as any)._droppedAttributesCount += projected.dropped;
        // Preserve standalone's combined OTel count, but authenticate the FI
        // contribution separately so it can never become host-loss evidence.
        rememberProjectedAttributeDrops(span, projected.dropped);
      }
      return new FISpan({ span, config: this.config, startAttributes, attributeCountLimit, projectedDropped: projected.dropped, autoDestination: this.autoDestination, startPrivacyHints, startAdmissionFailed });
    }
  }
