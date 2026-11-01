import {
  createOpenTelemetryInstrumentation,
  type ContentTransform,
} from '@flue/opentelemetry';
import { instrument } from '@flue/runtime';
import { createNoopMeter, diag, type Context } from '@opentelemetry/api';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { CompressionAlgorithm } from '@opentelemetry/otlp-exporter-base';
import {
  BasicTracerProvider,
  BatchSpanProcessor,
  type ReadableSpan,
  type Span,
  type SpanProcessor,
} from '@opentelemetry/sdk-trace-base';

export interface FlueTracingOptions {
  /** Defaults to FI_PROJECT_NAME, then flue-agent. */
  projectName?: string;
  /** Defaults to FI_API_KEY. Never added to spans. */
  apiKey?: string;
  /** Defaults to FI_SECRET_KEY. Never added to spans. */
  secretKey?: string;
  /** Origin only; defaults to FI_BASE_URL, then https://api.futureagi.com. */
  baseUrl?: string;
  /** Defaults to OTEL_SERVICE_NAME, then the project name. */
  serviceName?: string;
  /** Defaults to false (or FI_CAPTURE_CONTENT=1/true/yes/on). */
  captureContent?: boolean;
  /** Optional native content transform. Stack traces are always dropped first. */
  transform?: ContentTransform;
  /** Maximum wait per flush/shutdown, in milliseconds. Default 5000. */
  timeoutMillis?: number;
}

export interface FlueTracing {
  /** Deliver already-ended spans. Does not stop active runs. Rejects on failure. */
  flush(): Promise<void>;
  /** Unregister instrumentation, end remaining native spans and flush once. */
  shutdown(): Promise<void>;
}

// This processor only annotates spans created by Flue's official adapter.
// It never creates a span or changes native identity, timing or parent links.
class DestinationProcessor implements SpanProcessor {
  constructor(private readonly destination: SpanProcessor) {}

  onStart(span: Span, parentContext: Context): void {
    const operation = span.attributes['gen_ai.operation.name'];
    const kind = operation === 'invoke_agent' ? 'AGENT'
      : operation === 'chat' ? 'LLM'
        : operation === 'execute_tool' ? 'TOOL' : 'CHAIN';
    span.setAttribute('fi.span.kind', kind);
    const conversation = span.attributes['gen_ai.conversation.id'];
    if (typeof conversation === 'string' && conversation.length > 0) {
      span.setAttribute('session.id', conversation);
    }
    this.destination.onStart(span, parentContext);
  }

  onEnd(span: ReadableSpan): void { this.destination.onEnd(span); }
  forceFlush(): Promise<void> { return this.destination.forceFlush(); }
  shutdown(): Promise<void> { return this.destination.shutdown(); }
}

function bounded<T>(work: Promise<T>, timeoutMillis: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Flue tracing export timed out')), timeoutMillis);
    work.then(resolve, reject).finally(() => clearTimeout(timer));
  });
}

function required(value: string | undefined, name: string): string {
  if (!value?.trim()) throw new Error(`${name} must be set to configure Flue tracing`);
  return value.trim();
}

let active: { identity: string; transform?: ContentTransform; tracing: FlueTracing } | undefined;

/**
 * Register the official Flue adapter once with a private, traces-only provider.
 * No global OTel providers, signal handlers, resource detectors or model patches
 * are installed. Repeated identical calls return the same lifecycle handle.
 */
export function configure(options: FlueTracingOptions = {}): FlueTracing {
  const apiKey = required(options.apiKey ?? process.env.FI_API_KEY, 'FI_API_KEY');
  const secretKey = required(options.secretKey ?? process.env.FI_SECRET_KEY, 'FI_SECRET_KEY');
  const projectName = options.projectName?.trim() || process.env.FI_PROJECT_NAME?.trim() || 'flue-agent';
  const serviceName = options.serviceName?.trim() || process.env.OTEL_SERVICE_NAME?.trim() || projectName;
  const captureContent = options.captureContent ?? /^(1|true|yes|on)$/i.test((process.env.FI_CAPTURE_CONTENT ?? '').trim());
  const timeoutMillis = options.timeoutMillis ?? 5000;
  if (!Number.isSafeInteger(timeoutMillis) || timeoutMillis < 1 || timeoutMillis > 60000) {
    throw new Error('timeoutMillis must be an integer between 1 and 60000');
  }
  let base: URL;
  try {
    base = new URL(options.baseUrl ?? process.env.FI_BASE_URL ?? 'https://api.futureagi.com');
  } catch {
    throw new Error('FI_BASE_URL must be an HTTP(S) origin without credentials, path, query or fragment');
  }
  if (!['https:', 'http:'].includes(base.protocol) || base.username || base.password ||
      base.pathname !== '/' || base.search || base.hash) {
    throw new Error('FI_BASE_URL must be an HTTP(S) origin without credentials, path, query or fragment');
  }
  const identity = JSON.stringify([apiKey, secretKey, projectName, serviceName, captureContent, timeoutMillis, base.origin]);
  if (active) {
    if (active.identity !== identity || active.transform !== options.transform) {
      throw new Error('Flue tracing is already configured; shut it down before changing settings');
    }
    return active.tracing;
  }

  const processor = new DestinationProcessor(new BatchSpanProcessor(new OTLPTraceExporter({
    url: `${base.origin}/tracer/v1/traces`,
    headers: { 'X-Api-Key': apiKey, 'X-Secret-Key': secretKey },
    // Do not inherit gzip from a global OTEL_EXPORTER_OTLP_COMPRESSION setting.
    compression: CompressionAlgorithm.NONE,
    timeoutMillis,
  }), { exportTimeoutMillis: timeoutMillis }));
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({
      project_name: projectName,
      project_type: 'observe',
      'service.name': serviceName,
    }),
    spanProcessors: [processor],
  });
  const native = createOpenTelemetryInstrumentation({
    tracer: provider.getTracer('@flue/opentelemetry'),
    // The native default uses the global meter. Explicitly opt out even when
    // the application already has a global metrics exporter installed.
    meter: createNoopMeter(),
    content: captureContent ? {
      transform: (content, scope) => scope.contentType === 'exception_stacktrace'
        ? undefined : options.transform ? options.transform(content, scope) : content,
    } : false,
  });

  let finishPromise: Promise<void> | undefined;
  let shutdownPromise: Promise<void> | undefined;
  const pendingFlushes = new Set<Promise<void>>();
  const flush = (): Promise<void> => {
    if (finishPromise) return bounded(finishPromise, timeoutMillis);
    // A later submission can end while a prior batch is in flight. Do not
    // coalesce those calls: every call must flush its own ended spans.
    const pending = bounded(provider.forceFlush(), timeoutMillis);
    pendingFlushes.add(pending);
    void pending.then(() => pendingFlushes.delete(pending), () => pendingFlushes.delete(pending));
    return pending;
  };
  const beforeExit = (): void => { void tracing.shutdown().catch(reportFailure); };
  const finish = (): Promise<void> => {
    finishPromise ??= (async () => {
      process.removeListener('beforeExit', beforeExit);
      native.dispose();
      try {
        // The SDK does not include an already-exporting batch in shutdown.
        // Wait for a submission-triggered flush before shutting it down.
        await Promise.all([...pendingFlushes]);
      } finally {
        await bounded(provider.shutdown(), timeoutMillis);
      }
    })().finally(() => {
      if (active?.tracing === tracing) active = undefined;
    });
    return finishPromise;
  };
  let unregister: () => Promise<void>;
  const tracing: FlueTracing = {
    flush,
    shutdown: () => shutdownPromise ??= bounded(unregister(), timeoutMillis),
  };
  try {
    unregister = instrument({
      ...native,
      observe(event, context) {
        const result = native.observe(event, context);
        if (event.type === 'submission_settled') void flush().catch(reportFailure);
        return result;
      },
      dispose: finish,
    });
  } catch (error) {
    // A separate copy or a manually registered official adapter may already
    // own Flue's key. Preserve that registration and clean up only our provider.
    native.dispose();
    void provider.shutdown().catch(reportFailure);
    throw error;
  }
  active = { identity, transform: options.transform, tracing };
  process.once('beforeExit', beforeExit);
  return tracing;
}

function reportFailure(): void {
  // Never log exporter errors, which can include destination URLs or headers.
  diag.warn('Flue tracing could not finish exporting; check the collector and call flush() before stopping the process');
}
