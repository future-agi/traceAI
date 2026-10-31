import { otelIntegration } from 'eve/instrumentation/otel';
import { BatchSpanProcessor, InMemorySpanExporter } from '@opentelemetry/sdk-trace-base';
import { BatchSpanProcessor as Batch2, InMemorySpanExporter as Exporter2 } from 'otel2';
import { createEveDestination } from '../dist/src/index.js';

// Check the real Eve declarations against both OTel processor types, without casts.
otelIntegration(createEveDestination({
  processor: new BatchSpanProcessor(new InMemorySpanExporter()),
}));
otelIntegration(createEveDestination({
  processor: new Batch2(new Exporter2()), captureContent: true,
}));
