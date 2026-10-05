import { type Attributes, type Context, SpanStatusCode, diag, trace } from "@opentelemetry/api";
import {
  BatchSpanProcessor,
  type BufferConfig,
  type ReadableSpan,
  SimpleSpanProcessor,
  type Span,
  type SpanExporter,
  type SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { mapVoltAgentAttributes, reconcileOperationUsage } from "./mapping";

type Resource = ReadableSpan["resource"];

/**
 * Anything shaped like an OpenTelemetry SDK 2.x `BasicTracerProvider`, such as the
 * `FITracerProvider` returned by `register()` from `@traceai/fi-core`.
 */
export interface TracerProviderLike {
  forceFlush(): Promise<void>;
  shutdown?(): Promise<void>;
}

export interface FIVoltAgentSpanProcessorOptions {
  /**
   * The Future AGI tracer provider from `register({ ..., setGlobalTracerProvider: false })`.
   * Mapped spans carry its resource (`project_name`, `project_type`) and are exported through
   * its OTLP exporter (endpoint and `x-api-key` / `x-secret-key` headers from fi-core).
   */
  tracerProvider?: TracerProviderLike;
  /** Alternative to `tracerProvider`: export mapped spans through this exporter. */
  exporter?: SpanExporter;
  /** Exporter mode only: the resource to export with. Defaults to the VoltAgent span's own. */
  resource?: Resource;
  /**
   * Batch spans (default true) or export each span as it ends. With `tracerProvider`, batching
   * wraps the provider's exporter in this processor's own BatchSpanProcessor.
   */
  batch?: boolean;
  /** BatchSpanProcessor settings (SDK defaults: queue 2048, batch 512, delay 5000 ms). */
  batchConfig?: BufferConfig;
  /**
   * Export prompts, messages, instructions, tool arguments/results and retrieval queries.
   * Default false. VoltAgent itself has no such flag and always writes them; this processor
   * drops them from the Future AGI copy unless you opt in.
   */
  captureContent?: boolean;
  /**
   * How long an llm span waits for its operation's root span before it is exported without
   * usage reconciliation. Default 30000 ms.
   */
  usageReconciliationTimeoutMs?: number;
}

interface Delegate {
  processor: SpanProcessor;
  resource?: Resource;
  flush(): Promise<void>;
  shutdown(): Promise<void>;
}

interface PendingLLMSpan {
  original: ReadableSpan;
  source: Attributes;
  attributes: Attributes;
  isError: boolean;
}

interface PendingOperation {
  spans: PendingLLMSpan[];
  timer?: ReturnType<typeof setTimeout>;
}

const DEFAULT_RECONCILIATION_TIMEOUT_MS = 30_000;
const MAX_PENDING_OPERATIONS = 1_000;

let globalConflictWarned = false;

/**
 * Maps VoltAgent's native OpenTelemetry spans onto Future AGI / GenAI conventions and forwards
 * a mapped copy to the traceAI exporter.
 *
 * Append it to `ObservabilityConfig.spanProcessors`. The span VoltAgent hands to other processors
 * (VoltOps, local storage, websocket) is never modified: this processor exports a copy.
 */
export class FIVoltAgentSpanProcessor implements SpanProcessor {
  private readonly captureContent: boolean;
  private readonly reconciliationTimeoutMs: number;
  private readonly delegate: Delegate;
  private readonly pending = new Map<string, PendingOperation>();
  private isShutdown = false;

  constructor(options: FIVoltAgentSpanProcessorOptions) {
    if (!options || (!options.tracerProvider && !options.exporter)) {
      throw new Error("FIVoltAgentSpanProcessor needs a tracerProvider (from register()) or an exporter");
    }
    this.captureContent = options.captureContent === true;
    this.reconciliationTimeoutMs = options.usageReconciliationTimeoutMs ?? DEFAULT_RECONCILIATION_TIMEOUT_MS;
    this.delegate = options.tracerProvider
      ? delegateFromProvider(options.tracerProvider, options)
      : delegateFromExporter(options);
  }

  onStart(_span: Span, _parentContext: Context): void {
    // The mapped copy is built when the span ends; nothing to do at start.
  }

  onEnd(span: ReadableSpan): void {
    if (this.isShutdown) return;
    try {
      const source = span.attributes ?? {};
      const mapped = mapVoltAgentAttributes(source, { captureContent: this.captureContent });

      if (mapped.isModelCall && mapped.operationId) {
        this.hold(mapped.operationId, {
          original: span,
          source,
          attributes: mapped.attributes,
          isError: span.status?.code === SpanStatusCode.ERROR,
        });
        return;
      }

      if (mapped.isOperationRoot && mapped.operationId) {
        const operation = this.take(mapped.operationId);
        if (operation) {
          try {
            reconcileOperationUsage(source, operation.spans);
          } catch (error) {
            diag.warn("@traceai/voltagent: usage reconciliation failed", error);
          }
          for (const held of operation.spans) this.forward(held.original, held.attributes);
        }
      }

      this.forward(span, mapped.attributes);
    } catch (error) {
      diag.error("@traceai/voltagent: failed to map or export a VoltAgent span", error);
    }
  }

  async forceFlush(): Promise<void> {
    try {
      this.releaseAll();
      await this.delegate.flush();
    } catch (error) {
      diag.error("@traceai/voltagent: forceFlush failed", error);
    }
  }

  async shutdown(): Promise<void> {
    if (this.isShutdown) return;
    try {
      this.releaseAll();
      this.isShutdown = true;
      await this.delegate.shutdown();
    } catch (error) {
      this.isShutdown = true;
      diag.error("@traceai/voltagent: shutdown failed", error);
    }
  }

  private hold(operationId: string, span: PendingLLMSpan): void {
    let operation = this.pending.get(operationId);
    if (!operation) {
      if (this.pending.size >= MAX_PENDING_OPERATIONS) {
        const oldest = this.pending.keys().next().value;
        if (oldest !== undefined) this.release(oldest);
      }
      operation = { spans: [] };
      operation.timer = setTimeout(() => this.release(operationId), this.reconciliationTimeoutMs);
      (operation.timer as { unref?: () => void }).unref?.();
      this.pending.set(operationId, operation);
    }
    operation.spans.push(span);
  }

  private take(operationId: string): PendingOperation | undefined {
    const operation = this.pending.get(operationId);
    if (!operation) return undefined;
    if (operation.timer) clearTimeout(operation.timer);
    this.pending.delete(operationId);
    return operation;
  }

  /** Export held llm spans without reconciliation (root never arrived, flush, or shutdown). */
  private release(operationId: string): void {
    const operation = this.take(operationId);
    if (!operation) return;
    for (const held of operation.spans) this.forward(held.original, held.attributes);
  }

  private releaseAll(): void {
    for (const operationId of Array.from(this.pending.keys())) this.release(operationId);
  }

  private forward(span: ReadableSpan, attributes: Attributes): void {
    try {
      this.delegate.processor.onEnd(copySpan(span, attributes, this.delegate.resource ?? span.resource));
    } catch (error) {
      diag.error("@traceai/voltagent: exporter rejected a span", error);
    }
  }
}

/** A ReadableSpan with new attributes and resource; every other field reads through. */
export function copySpan(span: ReadableSpan, attributes: Attributes, resource: Resource): ReadableSpan {
  const spanContext = span.spanContext();
  const copy = {
    name: span.name,
    kind: span.kind,
    spanContext: () => spanContext,
    parentSpanContext: span.parentSpanContext,
    startTime: span.startTime,
    endTime: span.endTime,
    status: span.status,
    attributes,
    links: span.links,
    events: span.events,
    duration: span.duration,
    ended: span.ended,
    resource,
    instrumentationScope: span.instrumentationScope,
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
  } as ReadableSpan & Record<string, unknown>;
  // SDK 1.x field names, in case the exporter in use still reads them.
  const legacy = span as unknown as Record<string, unknown>;
  if (legacy.parentSpanId !== undefined) copy.parentSpanId = legacy.parentSpanId;
  if (legacy.instrumentationLibrary !== undefined) copy.instrumentationLibrary = legacy.instrumentationLibrary;
  return copy;
}

function delegateFromProvider(provider: TracerProviderLike, options: FIVoltAgentSpanProcessorOptions): Delegate {
  // OpenTelemetry SDK 2.x keeps these private; there is no public way to hand a finished span
  // from one provider to another provider's processors.
  const internals = provider as unknown as {
    _activeSpanProcessor?: SpanProcessor & { _spanProcessors?: unknown[] };
    _resource?: Resource;
    _config?: { resource?: Resource };
  };
  const active = internals._activeSpanProcessor;
  if (!active || typeof active.onEnd !== "function") {
    throw new Error(
      "FIVoltAgentSpanProcessor: tracerProvider has no span processor. Pass the provider returned by register() from @traceai/fi-core (OpenTelemetry SDK 2.x), or pass an exporter.",
    );
  }
  warnIfGlobalProvider(provider);
  const resource = internals._resource ?? internals._config?.resource;

  // register() attaches a SimpleSpanProcessor (one OTLP request per span; the OTLP exporter
  // rejects requests beyond 30 in flight, so a burst drops spans), and register({ batch: true })
  // does not replace it on SDK 2.x. Batch through the provider's own exporter instead.
  const exporters = (active._spanProcessors ?? [])
    .map((processor) => (processor as { _exporter?: SpanExporter })._exporter)
    .filter((exporter): exporter is SpanExporter => !!exporter && typeof exporter.export === "function");
  if (options.batch !== false && exporters.length === 1) {
    const batch = new BatchSpanProcessor(new SharedExporter(exporters[0]), options.batchConfig);
    return {
      processor: batch,
      resource,
      flush: () => batch.forceFlush(),
      // SharedExporter.shutdown only flushes: the exporter still belongs to the provider.
      shutdown: () => batch.shutdown(),
    };
  }

  return {
    processor: active,
    resource,
    flush: () => provider.forceFlush(),
    // The provider belongs to the caller (other instrumentations may share it); flush only.
    shutdown: () => provider.forceFlush(),
  };
}

/** Exports through an exporter this processor does not own; never shuts it down. */
class SharedExporter implements SpanExporter {
  constructor(private readonly inner: SpanExporter) {}
  export(spans: ReadableSpan[], resultCallback: Parameters<SpanExporter["export"]>[1]): void {
    this.inner.export(spans, resultCallback);
  }
  async shutdown(): Promise<void> {
    await this.forceFlush();
  }
  async forceFlush(): Promise<void> {
    await this.inner.forceFlush?.();
  }
}

function delegateFromExporter(options: FIVoltAgentSpanProcessorOptions): Delegate {
  const exporter = options.exporter as SpanExporter;
  const processor =
    options.batch === false ? new SimpleSpanProcessor(exporter) : new BatchSpanProcessor(exporter, options.batchConfig);
  return {
    processor,
    resource: options.resource,
    flush: () => processor.forceFlush(),
    shutdown: () => processor.shutdown(),
  };
}

/**
 * VoltAgentObservability registers its own NodeTracerProvider globally and takes its tracer from
 * the global API. If register() already made the Future AGI provider global, VoltAgent's
 * registration fails, its spans go straight to the Future AGI provider unmapped and with content,
 * and no span processor in ObservabilityConfig (this one or VoltOps) ever sees them.
 */
function warnIfGlobalProvider(provider: TracerProviderLike): void {
  try {
    const global = trace.getTracerProvider() as unknown as { getDelegate?: () => unknown };
    const active = typeof global.getDelegate === "function" ? global.getDelegate() : global;
    if (active === provider && !globalConflictWarned) {
      globalConflictWarned = true;
      const message =
        "@traceai/voltagent: the Future AGI tracer provider is the global OpenTelemetry provider. " +
        "VoltAgent's spans will bypass FIVoltAgentSpanProcessor (unmapped, content included). " +
        "Call register({ ..., setGlobalTracerProvider: false }).";
      diag.warn(message);
      // eslint-disable-next-line no-console
      console.warn(message);
    }
  } catch {
    // Diagnostics only.
  }
}
