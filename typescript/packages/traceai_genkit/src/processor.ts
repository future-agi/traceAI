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
import { getAttributesFromContext } from "@traceai/fi-core";
import { mapGenkitAttributes } from "./mapping";

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
   * Export Genkit's `genkit:input` / `genkit:output` (also copied to
   * `input.value` / `output.value`). Default false: content is dropped, and
   * the data Genkit's schema-validation errors embed after "Provided data:" is
   * cut from the span status message and exception events.
   */
  captureContent?: boolean;
  /** Upper bound for `forceFlush()` / `shutdown()`. Default 30000 ms. They resolve, never reject. */
  flushTimeoutMillis?: number;
}

const DEFAULT_FLUSH_TIMEOUT_MILLIS = 30_000;
const DEFAULT_SCOPE: InstrumentationScopeLike = { name: "genkit-tracer" };

/**
 * Genkit's schema ValidationError puts the rejected input or output after this
 * marker (`@genkit-ai/core` 1.42.0 src/schema.ts:78), and Genkit copies the
 * error message to the span status and the exception event
 * (core/src/tracing/instrumentation.ts:156-162).
 */
export const GENKIT_PROVIDED_DATA_MARKER = "Provided data:";
/** Replaces everything from {@link GENKIT_PROVIDED_DATA_MARKER} on when `captureContent` is off. */
export const ERROR_DATA_REDACTED_NOTE = "[data redacted: captureContent is off]";
const EXCEPTION_EVENT_NAME = "exception";
const EXCEPTION_TEXT_KEYS = ["exception.message", "exception.stacktrace"] as const;

/** Cut a Genkit error text at "Provided data:" and append the fixed note. Text without the marker is returned as is. */
export function redactErrorData(text: string): string {
  const at = text.indexOf(GENKIT_PROVIDED_DATA_MARKER);
  return at === -1 ? text : text.slice(0, at) + ERROR_DATA_REDACTED_NOTE;
}

function redactStatus(status: SpanStatus): SpanStatus {
  if (typeof status?.message !== "string") return status;
  const message = redactErrorData(status.message);
  return message === status.message ? status : { ...status, message };
}

/** New event objects for exception events whose message or stacktrace carries Genkit data; the rest are shared. */
function redactExceptionEvents(events: TimedEventLike[]): TimedEventLike[] {
  if (!Array.isArray(events)) return events;
  let changed = false;
  const out = events.map((event) => {
    if (event?.name !== EXCEPTION_EVENT_NAME || !event.attributes) return event;
    let attributes: Attributes | undefined;
    for (const key of EXCEPTION_TEXT_KEYS) {
      const value = event.attributes[key];
      if (typeof value !== "string") continue;
      const redacted = redactErrorData(value);
      if (redacted !== value) attributes = { ...(attributes ?? event.attributes), [key]: redacted };
    }
    if (!attributes) return event;
    changed = true;
    return { ...event, attributes };
  });
  return changed ? out : events;
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
  options: { captureContent?: boolean } = {},
): ExportableSpan {
  const ctx = span.spanContext();
  let parentSpanContext = span.parentSpanContext;
  const parentSpanId = parentSpanContext?.spanId ?? span.parentSpanId;
  if (!parentSpanContext && parentSpanId) {
    parentSpanContext = { traceId: ctx.traceId, spanId: parentSpanId, traceFlags: ctx.traceFlags, isRemote: false };
  }
  const scope = span.instrumentationScope ?? span.instrumentationLibrary ?? DEFAULT_SCOPE;
  const captureContent = options.captureContent === true;
  return {
    name: span.name,
    kind: span.kind,
    spanContext: () => ctx,
    parentSpanId,
    parentSpanContext,
    startTime: span.startTime,
    endTime: span.endTime,
    status: captureContent ? span.status : redactStatus(span.status),
    attributes,
    links: span.links,
    events: captureContent ? span.events : redactExceptionEvents(span.events),
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
 * Genkit keeps its own telemetry-server processor first in the list
 * (`@genkit-ai/core` tracing/node-telemetry-provider.ts:66-77), so the dev UI
 * keeps working. For each ended Genkit span this processor builds a copy with
 * Future AGI span kinds, model, usage and session mapped, content dropped
 * unless opted in, and the Future AGI resource (`project_name`,
 * `project_type`), then hands it to the `@traceai/fi-core` provider's span
 * processor and exporter. Genkit's span object is never modified.
 *
 * No method throws. Export failures are logged through the OTel diag logger.
 */
export class FIGenkitSpanProcessor {
  private readonly provider: FITracerProviderLike;
  private readonly target: TargetSpanProcessor;
  private readonly resource: unknown;
  private readonly captureContent: boolean;
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
    this.captureContent = options.captureContent === true;
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
        captureContent: this.captureContent,
        contextAttributes,
      });
      this.target.onEnd(toExportableSpan(span, attributes, this.resource, { captureContent: this.captureContent }));
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
