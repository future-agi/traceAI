import { syncBuiltinESMExports } from 'node:module';
import { diag, ProxyTracerProvider, SpanStatusCode, trace } from '@opentelemetry/api';
import { NodeSDK } from '@opentelemetry/sdk-node';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { CompressionAlgorithm } from '@opentelemetry/otlp-exporter-base';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { GraphQLInstrumentation } from '@opentelemetry/instrumentation-graphql';
import { HttpInstrumentation } from '@opentelemetry/instrumentation-http';
import { FiSanitizingExporter, type PrivacyOptions } from './privacy';

export { FiSanitizingExporter, sanitizeSpan, type PrivacyOptions } from './privacy';

export interface ApolloTracingOptions extends PrivacyOptions {
  projectName?: string;
  baseUrl?: string;
  apiKey?: string;
  secretKey?: string;
  /** Maximum resolver depth. Defaults to top-level fields only. */
  depth?: number;
}
export interface ApolloTracing {
  /** Export finished spans. False means export failed; no payload is logged. */
  forceFlush(): Promise<boolean>;
  /** Call after server.stop(). Idempotent; false means export failed. */
  shutdown(): Promise<boolean>;
}
let configured = false;

/** Call once, before importing Apollo or graphql. Owns this process's tracing SDK. */
export function configure(options: ApolloTracingOptions = {}): ApolloTracing {
  if (configured) throw new Error('Apollo tracing is already configured');
  if (Object.keys(require.cache).some(path => /[/\\]graphql[/\\](index|graphql)\.js$/.test(path))) {
    throw new Error('Initialize Apollo tracing before importing graphql or Apollo; use node --import');
  }
  const provider = trace.getTracerProvider();
  if (!(provider instanceof ProxyTracerProvider) ||
      provider.getDelegate() !== new ProxyTracerProvider().getDelegate()) {
    throw new Error('An OpenTelemetry tracer provider is already registered; do not create a second SDK');
  }
  if (process.env.OTEL_SDK_DISABLED?.toLowerCase() === 'true') {
    throw new Error('Apollo tracing cannot start while OTEL_SDK_DISABLED=true');
  }
  const projectName = options.projectName ?? process.env.FI_PROJECT_NAME ?? 'apollo-graphql';
  const apiKey = options.apiKey ?? process.env.FI_API_KEY;
  const secretKey = options.secretKey ?? process.env.FI_SECRET_KEY;
  if (!projectName.trim() || !apiKey?.trim() || !secretKey?.trim()) {
    throw new Error('Set FI_PROJECT_NAME, FI_API_KEY and FI_SECRET_KEY, or pass their configure options');
  }
  const baseUrl = new URL(options.baseUrl ?? process.env.FI_BASE_URL ?? 'https://api.futureagi.com');
  if (!['http:', 'https:'].includes(baseUrl.protocol) || baseUrl.username || baseUrl.password ||
      baseUrl.search || baseUrl.hash) {
    throw new Error('baseUrl must be an HTTP(S) URL without credentials, query or fragment');
  }
  const depth = options.depth ?? 1;
  if (!Number.isInteger(depth) || depth < 1) throw new Error('depth must be a positive integer');
  const processor = new BatchSpanProcessor({ exporter: new FiSanitizingExporter(new OTLPTraceExporter({
    url: `${baseUrl.href.replace(/\/+$/, '')}/tracer/v1/traces`,
    headers: { 'X-Api-Key': apiKey, 'X-Secret-Key': secretKey },
    compression: CompressionAlgorithm.NONE,
    timeoutMillis: 3000,
  }), {
    captureContent: options.captureContent ?? process.env.FI_APOLLO_CAPTURE_CONTENT === 'true',
  }), exportTimeoutMillis: 4000 });
  const instrumentations = [
    new HttpInstrumentation(),
    new GraphQLInstrumentation({
      depth,
      allowValues: false,
      responseHook: (span, result) => {
        if (result?.errors?.length) span.setStatus({ code: SpanStatusCode.ERROR });
      },
    }),
  ];
  const sdk = new NodeSDK({
    autoDetectResources: false,
    resourceDetectors: [],
    resource: resourceFromAttributes({
      project_name: projectName, project_type: 'observe', 'service.name': projectName,
    }),
    spanProcessors: [processor],
    metricReaders: [],
    logRecordProcessors: [],
    instrumentations,
  });
  sdk.start();
  // Apollo 5 standalone uses the ESM named createServer binding. OTel patches the
  // CommonJS HTTP module; refresh its ESM exports, rather than inventing a root span.
  require('node:http');
  require('node:https');
  syncBuiltinESMExports();
  configured = true;
  let shutdown: Promise<boolean> | undefined;
  const safely = async (operation: () => Promise<void>): Promise<boolean> => {
    try {
      await operation();
      return true;
    } catch {
      // Error text can contain application data; diagnostics must remain metadata-only.
      diag.warn('Apollo telemetry export failed');
      return false;
    }
  };
  return {
    forceFlush: () => shutdown ?? safely(() => processor.forceFlush()),
    shutdown: () => {
      if (!shutdown) {
        instrumentations.forEach(instrumentation => instrumentation.disable());
        syncBuiltinESMExports();
        shutdown = safely(() => sdk.shutdown());
      }
      return shutdown;
    },
  };
}
