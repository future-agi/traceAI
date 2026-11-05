// Minimal prior recipe: stock instrumentation and privacy exporter, without the
// ESM HTTP binding repair. This is a regression control, never a customer setup.
import { NodeSDK } from '@opentelemetry/sdk-node';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { GraphQLInstrumentation } from '@opentelemetry/instrumentation-graphql';
import { HttpInstrumentation } from '@opentelemetry/instrumentation-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { FiSanitizingExporter } from '@traceai/apollo-graphql';
const sdk = new NodeSDK({
  autoDetectResources: false, metricReaders: [], logRecordProcessors: [],
  resource: resourceFromAttributes({ project_name: 'apollo-offline', project_type: 'observe' }),
  spanProcessors: [new BatchSpanProcessor({ exporter: new FiSanitizingExporter(new OTLPTraceExporter({
    url: `${process.env.FI_BASE_URL}/tracer/v1/traces`,
  })) })],
  instrumentations: [new HttpInstrumentation(), new GraphQLInstrumentation({ depth: 1, allowValues: false })],
});
sdk.start();
let shutdown;
globalThis.telemetry = { shutdown: () => shutdown ??= sdk.shutdown().then(() => true) };
