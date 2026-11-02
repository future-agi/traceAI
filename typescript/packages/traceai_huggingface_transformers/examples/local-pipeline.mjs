import path from 'node:path';
import * as transformers from '@huggingface/transformers';
import { TransformersInstrumentation } from '@traceai/huggingface-transformers';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';

if (!process.env.FI_API_KEY || !process.env.FI_SECRET_KEY) {
  throw new Error('Set FI_API_KEY and FI_SECRET_KEY before starting the application.');
}
const provider = new NodeTracerProvider({
  resource: resourceFromAttributes({
    'service.name': 'transformers-example',
    project_name: 'transformers-example',
    project_type: 'observe',
  }),
  spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({
    url: process.env.FI_TRACE_ENDPOINT ?? 'https://api.futureagi.com/tracer/v1/traces',
    headers: {
      FI_API_KEY: process.env.FI_API_KEY,
      FI_SECRET_KEY: process.env.FI_SECRET_KEY,
    },
  }))],
});
provider.register();

// Optional: use model files already deployed with your application.
if (process.env.MODEL_DIRECTORY) {
  transformers.env.localModelPath = path.resolve(process.env.MODEL_DIRECTORY) + path.sep;
  transformers.env.allowRemoteModels = false;
}
const instrumentation = new TransformersInstrumentation();
const { pipeline } = instrumentation.wrapModule(transformers);
let extractor;
try {
  extractor = await pipeline('feature-extraction', process.env.MODEL_ID ?? 'Xenova/all-MiniLM-L6-v2', {
    device: 'cpu', dtype: 'fp32',
  });
  const embeddings = await extractor(['How do I reset my password?', 'Update account settings'], {
    pooling: 'mean', normalize: true,
  });
  console.log('Embedding shape:', embeddings.dims);
} finally {
  await extractor?.dispose();
  await provider.forceFlush();
  await provider.shutdown();
}
