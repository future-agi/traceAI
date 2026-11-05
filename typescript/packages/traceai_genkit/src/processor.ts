import {
  diag,
  type Attributes,
  type AttributeValue,
  type Context,
  type HrTime,
  type Link,
  type SpanContext,
  type SpanKind,
  type SpanStatus,
  trace,
} from "@opentelemetry/api";
import { getAttributesFromContext, type TraceConfigOptions } from "@traceai/fi-core";
import { mapGenkitAttributes } from "./mapping";
import {
  type ContentPolicy,
  defaultScrubContext,
  hidesFreeText,
  isErrorDetailAttribute,
  resolveContentPolicy,
  SHOW_ALL,
  type ScrubContext,
  scrubContent,
  scrubErrorText,
} from "./privacy";

/** Instrumentation scope as both sdk-trace-base 1.x (`instrumentationLibrary`) and 2.x (`instrumentationScope`) carry it. */
export interface InstrumentationScopeLike {
  readonly name: string;
  readonly version?: string;
  readonly schemaUrl?: string;
}

export interface TimedEventLike {
  readonly time: HrTime;
  readonly name: string;
  readonly attributes?: Attributes;
  readonly droppedAttributesCount?: number;
}

/**
 * The parts of an OpenTelemetry ReadableSpan this processor reads.
 *
 * genkit 1.42.0 runs its NodeSDK on @opentelemetry/sdk-trace-base 1.25, whose
 * spans carry `parentSpanId` and `instrumentationLibrary`. @traceai/fi-core
 * exports with sdk-trace-base / OTLP transformer 2.x, which read
 * `parentSpanContext` and `instrumentationScope`. Both shapes are accepted.
 */
export interface GenkitReadableSpan {
  readonly name: string;
  readonly kind: SpanKind;
  spanContext(): SpanContext;
  readonly parentSpanId?: string;
  readonly parentSpanContext?: SpanContext;
  readonly startTime: HrTime;
  readonly endTime: HrTime;
  readonly status: SpanStatus;
  readonly attributes: Attributes;
  readonly links: Link[];
  readonly events: TimedEventLike[];
  readonly duration: HrTime;
  readonly ended: boolean;
  readonly instrumentationLibrary?: InstrumentationScopeLike;
  readonly instrumentationScope?: InstrumentationScopeLike;
  readonly droppedAttributesCount?: number;
  readonly droppedEventsCount?: number;
  readonly droppedLinksCount?: number;
}

/** The span shape handed to the Future AGI tracer provider's span processor (sdk-trace-base 2.x ReadableSpan). */
export interface ExportableSpan extends GenkitReadableSpan {
  readonly parentSpanContext: SpanContext | undefined;
  readonly instrumentationScope: InstrumentationScopeLike;
  readonly instrumentationLibrary: InstrumentationScopeLike;
  readonly resource: unknown;
  readonly droppedAttributesCount: number;
  readonly droppedEventsCount: number;
  readonly droppedLinksCount: number;
}

interface TargetSpanProcessor {
  onEnd(span: ExportableSpan): void;
  forceFlush(): Promise<void>;
  shutdown(): Promise<void>;
}

/** A tracer provider from `@traceai/fi-core` `register()` (an sdk-trace-base BasicTracerProvider). */
export interface FITracerProviderLike {
  forceFlush(): Promise<void>;
  shutdown(): Promise<void>;
}

export interface FIGenkitSpanProcessorOptions {
  /**
   * The provider returned by `register()` from `@traceai/fi-core`. Create it with
   * `setGlobalTracerProvider: false`: Genkit's NodeSDK must own the global
   * provider, or Genkit's spans bypass every processor passed to
   * `enableTelemetry`.
   */
  tracerProvider: FITracerProviderLike;
  /**
   * traceAI content switches, as for every traceAI instrumentor. Content (Genkit's
   * `genkit:input` / `genkit:output`, copied to `input.value` / `output.value`) is
   * exported by default; `hideInputs` / `hideOutputs` (or `FI_HIDE_INPUTS=true` /
   * `FI_HIDE_OUTPUTS=true`) hide it. Message/text flags apply to model content.
   * `hideErrorDetails` (or `FI_HIDE_ERROR_DETAILS`) hides status descriptions and error
   * detail attributes; when unset, any input/output/message/text hide flag enables it.
   * An explicit false opts out. Precedence: option > env var > default. The env vars
   * apply even when this option is not passed.
   */
  traceConfig?: TraceConfigOptions;
  /**
   * Removed. Use `traceConfig` or the FI_HIDE_* env vars. Passing it logs a warning;
   * `false` still hides inputs and outputs (unless `traceConfig` or the env vars say
   * otherwise), so an old configuration never exports more than it asked for.
   * @deprecated
   */
  captureContent?: boolean;
  /** Upper bound for `forceFlush()` / `shutdown()`. Default 30000 ms. They resolve, never reject. */
  flushTimeoutMillis?: number;
}

const DEFAULT_FLUSH_TIMEOUT_MILLIS = 30_000;
const DEFAULT_SCOPE: InstrumentationScopeLike = { name: "genkit-tracer" };

const EXCEPTION_EVENT_NAME = "exception";
const EXCEPTION_MESSAGE = "exception.message";
/** Raw stack traces (local paths, module layout) are never exported. */
const EXCEPTION_STACKTRACE = "exception.stacktrace";

export interface ExportPrivacy {
  policy?: ContentPolicy;
  scrub?: ScrubContext;
}

/**
 * With error details hidden, keep only the status code, as fi-core does. Otherwise export
 * the message without local paths, stack traces, emails, or this machine's user and host names.
 * The events argument is retained for API compatibility.
 */
export function exportStatus(status: SpanStatus, _events: TimedEventLike[] | undefined, privacy: ExportPrivacy = {}): SpanStatus {
  if (typeof status?.message !== "string") return status;
  const policy = privacy.policy ?? SHOW_ALL;
  if (hidesFreeText(policy)) return { code: status.code };
  const message = scrubErrorText(status.message, privacy.scrub);
  return message === status.message ? status : { ...status, message };
}

/**
 * The exported events. Every `exception` event loses `exception.stacktrace`; its message follows
 * the status rule above. String attributes of other events are scrubbed of local paths and stack
 * traces. Genkit's own event objects are never modified.
 */
export function exportEvents(events: TimedEventLike[], privacy: ExportPrivacy = {}): TimedEventLike[] {
  if (!Array.isArray(events)) return events;
  const hideErrors = hidesFreeText(privacy.policy ?? SHOW_ALL);
  return events.map((event) => {
    if (!event?.attributes) return event;
    const isException = event.name === EXCEPTION_EVENT_NAME;
    const attributes: Attributes = {};
    for (const [key, value] of Object.entries(event.attributes)) {
      if (value === undefined) continue;
      if (hideErrors && isErrorDetailAttribute(key)) continue;
      if (isException && key === EXCEPTION_STACKTRACE) continue;
      if (isException && key === EXCEPTION_MESSAGE && typeof value === "string") {
        attributes[key] = scrubErrorText(value, privacy.scrub);
        continue;
      }
      attributes[key] =
        typeof value === "string"
          ? isException
            ? scrubErrorText(value, privacy.scrub)
            : scrubContent(value, privacy.scrub)
          : value;
    }
    return { ...event, attributes };
  });
}

function resolveTarget(provider: FITracerProviderLike): { processor: TargetSpanProcessor; resource: unknown } {
  // sdk-trace-base 2.x keeps these as TypeScript-private fields; 1.x exposed
  // `activeSpanProcessor` and `resource`. There is no public accessor in either.
  const p = provider as unknown as Record<string, unknown> & { _config?: { resource?: unknown } };
  const processor = (p._activeSpanProcessor ?? p.activeSpanProcessor) as TargetSpanProcessor | undefined;
  const resource = p._resource ?? p.resource ?? p._config?.resource;
  if (!processor || typeof processor.onEnd !== "function" || typeof processor.forceFlush !== "function" || !resource) {
    throw new TypeError(
      "FIGenkitSpanProcessor: tracerProvider must be the provider returned by register() from @traceai/fi-core " +
        "(an @opentelemetry/sdk-trace-base BasicTracerProvider).",
    );
  }
  return { processor, resource };
}

function isGlobalTracerProvider(provider: unknown): boolean {
  try {
    const global = trace.getTracerProvider() as { getDelegate?: () => unknown };
    return typeof global.getDelegate === "function" && global.getDelegate() === provider;
  } catch {
    return false;
  }
}

/** Build the sdk-trace-base 2.x view of a span with new attributes and the Future AGI resource. */
export function toExportableSpan(
  span: GenkitReadableSpan,
  attributes: Attributes,
  resource: unknown,
  privacy: ExportPrivacy = {},
): ExportableSpan {
  const ctx = span.spanContext();
  let parentSpanContext = span.parentSpanContext;
  const parentSpanId = parentSpanContext?.spanId ?? span.parentSpanId;
  if (!parentSpanContext && parentSpanId) {
    parentSpanContext = { traceId: ctx.traceId, spanId: parentSpanId, traceFlags: ctx.traceFlags, isRemote: false };
  }
  const scope = span.instrumentationScope ?? span.instrumentationLibrary ?? DEFAULT_SCOPE;
  return {
    name: span.name,
    kind: span.kind,
    spanContext: () => ctx,
    parentSpanId,
    parentSpanContext,
    startTime: span.startTime,
    endTime: span.endTime,
    status: exportStatus(span.status, span.events, privacy),
    attributes,
    links: span.links,
    events: exportEvents(span.events, privacy),
    duration: span.duration,
    ended: span.ended,
    resource,
    instrumentationScope: scope,
    instrumentationLibrary: scope,
    droppedAttributesCount: span.droppedAttributesCount ?? 0,
    droppedEventsCount: span.droppedEventsCount ?? 0,
    droppedLinksCount: span.droppedLinksCount ?? 0,
  };
}

function withTimeout(promise: Promise<unknown>, millis: number, what: string): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(() => {
      diag.warn(`FIGenkitSpanProcessor: ${what} did not finish within ${millis} ms`);
      resolve();
    }, millis);
    if (typeof timer === "object" && timer && "unref" in timer) timer.unref();
  });
  const settled = promise.then(
    () => undefined,
    (error: unknown) => {
      diag.warn(`FIGenkitSpanProcessor: ${what} failed: ${error}`);
    },
  );
  return Promise.race([settled, timeout]).finally(() => {
    if (timer !== undefined) clearTimeout(timer);
  });
}

function hasAttributes(attributes: Attributes): boolean {
  for (const value of Object.values(attributes) as (AttributeValue | undefined)[]) {
    if (value !== undefined) return true;
  }
  return false;
}

/**
 * A span processor for Genkit's `enableTelemetry({ spanProcessors })`.
 *
 * For each ended Genkit span this processor builds a copy with
 * Future AGI span kinds, model, usage and session mapped, content handled per
 * the traceAI TraceConfig (exported by default, FI_HIDE_* hides it), stack
 * traces and local paths removed, and the Future AGI resource (`project_name`,
 * `project_type`), then hands it to the `@traceai/fi-core` provider's span
 * processor and exporter. Genkit's span object is never modified.
 *
 * No method throws. Export failures are logged through the OTel diag logger.
 */
export class FIGenkitSpanProcessor {
  private readonly provider: FITracerProviderLike;
  private readonly target: TargetSpanProcessor;
  private readonly resource: unknown;
  private readonly policy: ContentPolicy;
  private readonly scrub: ScrubContext;
  private readonly flushTimeoutMillis: number;
  private readonly contextAttributes = new WeakMap<object, Attributes>();
  private isShutdown = false;
  private shutdownPromise: Promise<void> | undefined;

  constructor(options: FIGenkitSpanProcessorOptions) {
    if (!options || !options.tracerProvider) {
      throw new TypeError("FIGenkitSpanProcessor: options.tracerProvider is required (register() from @traceai/fi-core).");
    }
    this.provider = options.tracerProvider;
    const { processor, resource } = resolveTarget(options.tracerProvider);
    this.target = processor;
    this.resource = resource;
    if (options.captureContent !== undefined) {
      diag.warn(
        "@traceai/genkit: captureContent was removed. Content follows the traceAI TraceConfig: exported by default, " +
          "hidden with traceConfig ({ hideInputs, hideOutputs, ... }) or FI_HIDE_INPUTS / FI_HIDE_OUTPUTS.",
      );
    }
    this.policy = resolveContentPolicy(
      options.traceConfig,
      options.captureContent === false ? { hideInputs: true, hideOutputs: true } : {},
    );
    this.scrub = defaultScrubContext();
    this.flushTimeoutMillis = options.flushTimeoutMillis ?? DEFAULT_FLUSH_TIMEOUT_MILLIS;
    if (isGlobalTracerProvider(options.tracerProvider)) {
      const message =
        "@traceai/genkit: the Future AGI tracer provider is the global OpenTelemetry provider. Genkit's NodeSDK " +
        "cannot register its own, so Genkit spans bypass FIGenkitSpanProcessor and Genkit's dev UI exporter. " +
        "Call register({ ..., setGlobalTracerProvider: false }).";
      diag.warn(message);
      process.emitWarning(message, { code: "TRACEAI_GENKIT_GLOBAL_PROVIDER" });
    }
  }

  onStart(span: object, parentContext: Context): void {
    if (this.isShutdown) return;
    try {
      // Session, user, metadata and tags set with the fi-core context helpers
      // around a flow call. Genkit creates its spans with that context.
      const attributes = getAttributesFromContext(parentContext);
      if (hasAttributes(attributes)) {
        this.contextAttributes.set(span, attributes);
      }
    } catch (error) {
      diag.debug(`FIGenkitSpanProcessor.onStart: ${error}`);
    }
  }

  onEnd(span: GenkitReadableSpan): void {
    if (this.isShutdown) return;
    try {
      const contextAttributes = this.contextAttributes.get(span);
      const attributes = mapGenkitAttributes(span.attributes, {
        policy: this.policy,
        scrub: this.scrub,
        contextAttributes,
      });
      this.target.onEnd(toExportableSpan(span, attributes, this.resource, { policy: this.policy, scrub: this.scrub }));
    } catch (error) {
      diag.warn(`FIGenkitSpanProcessor.onEnd: span not exported: ${error}`);
    }
  }

  /** Wait for spans handed to the Future AGI provider to be exported. Resolves even when export fails. */
  forceFlush(): Promise<void> {
    let flushing: Promise<unknown>;
    try {
      flushing = this.provider.forceFlush();
    } catch (error) {
      flushing = Promise.reject(error);
    }
    return withTimeout(flushing, this.flushTimeoutMillis, "forceFlush");
  }

  /** Flush, then shut down the Future AGI provider. Called by Genkit's NodeSDK shutdown. Resolves, never rejects. */
  shutdown(): Promise<void> {
    if (this.shutdownPromise) return this.shutdownPromise;
    this.shutdownPromise = (async () => {
      await this.forceFlush();
      this.isShutdown = true;
      let closing: Promise<unknown>;
      try {
        closing = this.provider.shutdown();
      } catch (error) {
        closing = Promise.reject(error);
      }
      await withTimeout(closing, this.flushTimeoutMillis, "shutdown");
    })();
    return this.shutdownPromise;
  }
}
