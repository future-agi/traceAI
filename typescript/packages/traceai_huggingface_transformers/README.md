# Transformers.js tracing

Trace local Node.js Transformers.js pipeline calls in Future AGI with `@traceai/huggingface-transformers`. This package is separate from `@traceai/huggingface`, which traces the hosted Inference API.

## Install

Use Node.js 22.13.1 or later and `@huggingface/transformers` 3.x or 4.x. The adapter supports Node CPU execution, not browser or GPU execution. Node 22.13.1 is the minimum for the CommonJS path as well as ESM.

```sh
npm install @traceai/huggingface-transformers @huggingface/transformers \
  @opentelemetry/api @opentelemetry/sdk-trace-node@^2 \
  @opentelemetry/sdk-trace-base@^2 @opentelemetry/resources@^2 \
  @opentelemetry/exporter-trace-otlp-http@^0.202.0
```

Configure your existing OpenTelemetry provider before creating the instrumentor. If you do not have a provider, use the complete [local pipeline example](./examples/local-pipeline.mjs) with `FI_API_KEY` and `FI_SECRET_KEY` supplied through your environment. It sends OTLP/HTTP traces to `https://api.futureagi.com/tracer/v1/traces` and flushes before exit. Do not register a second provider in an application that already owns one.

```js
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
```

`wrapModule` returns a facade; use its `pipeline` factory instead of the original module's factory. It does not mutate ESM exports or patch global constructors. For a directly constructed or already loaded pipeline, use `instrumentation.wrapPipeline(existingPipeline)` and call the returned value. Wrapping the same value again is idempotent; the first instrumentor's policy remains in effect. `disable()` and `enable()` control that instrumentor's existing wrappers.

## What is captured

| Pipeline task | Span type | Content |
| --- | --- | --- |
| `text-generation` | LLM | Text, chat messages or batches; generated results; numeric/boolean generation settings including `max_new_tokens` |
| `text2text-generation` | LLM | Inputs and generated results |
| `summarization` | LLM | Inputs and summaries |
| `feature-extraction` | EMBEDDING | Inputs, output shape, batch size and embedding dimension; no vectors |
| `question-answering` | CHAIN | Separate question/context arguments and answer results |

Each supported invocation creates one local `INTERNAL` span, including batched calls. Model loading, downloads and `dispose()` do not create inference spans. Other tasks pass through unchanged. A wrapped factory records the requested model identifier; a directly wrapped pipeline uses its model configuration's `_name_or_path` when available. No token counts, cost, finish reasons or first-token timings are inferred.

`TextStreamer` callbacks, interruption criteria and final return objects remain owned by Transformers.js. The invocation span stays open until the pipeline settles, including callbacks. Original exceptions are rethrown unchanged.

## Control sensitive data

Use the shared `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_EMBEDDING_VECTORS` and `FI_HIDE_ERROR_DETAILS` environment settings, or pass `traceConfig` to the constructor. Set environment flags before constructing the instrumentor.

```js
const instrumentation = new TransformersInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
});
```

Input-hiding flags also omit output content because generated text and answers can echo the input. All embedding vectors are omitted regardless of configuration; only numeric shape information is retained. Content attributes are limited to 8 KiB each and oversized content is omitted. The adapter only reads allowlisted content and generation fields, never arbitrary options, credentials, model weights, tokenizer graphs or callbacks. Content-hiding flags imply error-detail hiding unless you explicitly override `hideErrorDetails`/`FI_HIDE_ERROR_DETAILS`.

## View and troubleshoot

Open **Observe** in Future AGI and select the project configured in your provider's `project_name` resource attribute. Look for `transformers.feature-extraction` or the other task-specific span names.

- **No invocation spans:** initialize your provider first and call the returned wrapped factory or pipeline, not the original value. Unsupported task types are intentionally not traced.
- **Missing spans on exit:** await the provider's `forceFlush()` and `shutdown()` after inference completes.
- **Missing content:** check the shared privacy settings and the 8 KiB per-content-attribute limit. Embedding vectors are intentionally never exported.
- **Using a local model directory:** set `env.localModelPath` to the directory containing your model folders and disable remote loading with `env.allowRemoteModels = false`. Select a folder name as the model identifier.
