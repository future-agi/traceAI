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
import type { TraceConfigOptions } from "@traceai/fi-core";
import { providerForAgentModel } from "./agentModels";
import { FIAttributes, VoltAgentAttributes as VA } from "./attributes";
import {
  type MapOptions,
  copyOperationOutput,
  mapSpanEvents,
  mapVoltAgentAttributes,
  reconcileOperationUsage,
} from "./mapping";
import { type ContentPolicy, defaultScrubContext, hidesFreeText, resolveContentPolicy, scrubErrorText } from "./privacy";

type Resource = ReadableSpan["resource"];
type SpanEvents = ReadableSpan["events"];
type SpanStatus = ReadableSpan["status"];

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
   * fi-core trace config, with the same defaults as every traceAI instrumentor: content (prompts,
   * messages, instructions, tool arguments/results, retrieval queries, answers) is exported unless
   * you hide it. `hideInputs` / `hideOutputs` (or `FI_HIDE_INPUTS=true` / `FI_HIDE_OUTPUTS=true`)
   * hide every input-side / output-side value and the run's error text; `hideInputMessages` /
   * `hideOutputMessages` hide the message lists; `hideInputText` / `hideOutputText` keep the
   * message roles and replace the text with `__REDACTED__`. Option > env var > default.
   */
  traceConfig?: TraceConfigOptions;
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
  events: SpanEvents;
  status: SpanStatus;
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
  private readonly policy: ContentPolicy;
  private readonly mapOptions: MapOptions;
  private readonly reconciliationTimeoutMs: number;
  private readonly delegate: Delegate;
  private readonly pending = new Map<string, PendingOperation>();
  private isShutdown = false;

  constructor(options: FIVoltAgentSpanProcessorOptions) {
    if (!options || (!options.tracerProvider && !options.exporter)) {
      throw new Error("FIVoltAgentSpanProcessor needs a tracerProvider (from register()) or an exporter");
    }
    this.policy = resolveContentPolicy(options.traceConfig);
    this.mapOptions = { policy: this.policy, scrub: defaultScrubContext() };
    if ((options as { captureContent?: unknown }).captureContent !== undefined) {
      diag.warn(
        "@traceai/voltagent: captureContent was removed. Content is exported by default; hide it with " +
          "traceConfig ({ hideInputs, hideOutputs, ... }) or FI_HIDE_INPUTS / FI_HIDE_OUTPUTS.",
      );
    }
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
      const options = this.mapOptions;
      const mapped = mapVoltAgentAttributes(source, options);
      if (mapped.isModelCall || mapped.isOperationRoot) {
        setProviderFromModelObject(source, mapped.attributes, mapped.isModelCall);
      }
      // Event attributes carry content too (guardrail stream chunks, workflow suspend/resume data)
      // and exception events carry stack traces.
      const events = mapSpanEvents(span.events, options);
      const status = this.exportStatus(span, source);

      if (mapped.isModelCall && mapped.operationId) {
        this.hold(mapped.operationId, {
          original: span,
          source,
          attributes: mapped.attributes,
          events,
          status,
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
          try {
            // Reads the mapped root: no output when outputs are hidden.
            copyOperationOutput(mapped.attributes, operation.spans);
          } catch (error) {
            diag.warn("@traceai/voltagent: copying the operation output failed", error);
          }
          for (const held of operation.spans) this.forward(held.original, held.attributes, held.events, held.status);
        }
      }

      this.forward(span, mapped.attributes, events, status);
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
    for (const held of operation.spans) this.forward(held.original, held.attributes, held.events, held.status);
  }

  private releaseAll(): void {
    for (const operationId of Array.from(this.pending.keys())) this.release(operationId);
  }

  private forward(span: ReadableSpan, attributes: Attributes, events: SpanEvents, status: SpanStatus): void {
    try {
      this.delegate.processor.onEnd(
        copySpan(span, attributes, this.delegate.resource ?? span.resource, events, status),
      );
    } catch (error) {
      diag.error("@traceai/voltagent: exporter rejected a span", error);
    }
  }

  /**
   * The status message is the thrown error's text (a tool error naming its argument, an API error
   * with a local path). Exported with paths, emails and local user/host names removed; when inputs
   * or outputs are hidden, an ERROR status carries the error type only.
   */
  private exportStatus(span: ReadableSpan, source: Attributes): SpanStatus {
    const status = span.status;
    if (!status || typeof status.message !== "string" || status.message.length === 0) return status;
    if (!hidesFreeText(this.policy)) return { ...status, message: scrubErrorText(status.message, this.mapOptions.scrub) };
    const errorType = status.code === SpanStatusCode.ERROR ? errorTypeOf(span, source) : undefined;
    return errorType ? { ...status, message: errorType } : { code: status.code };
  }
}

/** The error's class name, from VoltAgent's `error.type` or the exception event's `exception.type`. */
function errorTypeOf(span: ReadableSpan, source: Attributes): string | undefined {
  const fromAttribute = source["error.type"];
  if (typeof fromAttribute === "string" && /^[\w.$-]{1,80}$/.test(fromAttribute)) return fromAttribute;
  for (const event of span.events ?? []) {
    const type = event.attributes?.["exception.type"];
    if (typeof type === "string" && /^[\w.$-]{1,80}$/.test(type)) return type;
  }
  return undefined;
}

/** Fill gen_ai.provider.name from the agent's AI SDK model object when VoltAgent set none. */
function setProviderFromModelObject(source: Attributes, attributes: Attributes, isModelCall: boolean): void {
  if (attributes[FIAttributes.GEN_AI_PROVIDER_NAME] !== undefined) return;
  const agentId = source[VA.ENTITY_ID];
  if (typeof agentId !== "string" || agentId.length === 0) return;
  const modelKey = isModelCall ? source[VA.LLM_MODEL] : source[VA.AI_MODEL_NAME];
  try {
    const provider = providerForAgentModel(agentId, typeof modelKey === "string" ? modelKey : undefined);
    if (provider !== undefined) attributes[FIAttributes.GEN_AI_PROVIDER_NAME] = provider;
  } catch (error) {
    diag.debug("@traceai/voltagent: provider lookup failed", error);
  }
}

/**
 * A ReadableSpan with new attributes, resource, events and status; every other field reads
 * through. Pass events filtered with `mapSpanEvents` (the span's own events still carry content
 * and `exception.stacktrace`).
 */
export function copySpan(
  span: ReadableSpan,
  attributes: Attributes,
  resource: Resource,
  events: SpanEvents,
  status: SpanStatus = span.status,
): ReadableSpan {
  const spanContext = span.spanContext();
  const copy = {
    name: span.name,
    kind: span.kind,
    spanContext: () => spanContext,
    parentSpanContext: span.parentSpanContext,
    startTime: span.startTime,
    endTime: span.endTime,
    status,
    attributes,
    links: span.links,
    events,
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
