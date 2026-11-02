const assert = require('node:assert/strict');
const { test } = require('node:test');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { HuggingFaceInstrumentation } = require('../dist/cjs');

test('automatic require hook, repeated enable and disable, no duplicate spans', async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new HuggingFaceInstrumentation();
  instrumentation.setTracerProvider(provider);
  const input = { model: 'offline-model', endpointUrl: 'http://localhost:9999', inputs: 'offline prompt' };
  const fetch = async () => new Response(JSON.stringify([{ generated_text: 'offline answer' }]), { headers: { 'content-type': 'application/json' } });
  try {
    // Intentionally require the real dependency only after the hook is enabled.
    const sdk = require('@huggingface/inference');
    const client = new sdk.HfInference('', { fetch });
    await client.textGeneration(input);
    assert.equal(exporter.getFinishedSpans().length, 1);
    instrumentation.disable();
    await client.textGeneration(input);
    assert.equal(exporter.getFinishedSpans().length, 1);
    instrumentation.enable();
    const nextSdk = require('@huggingface/inference');
    await new nextSdk.HfInference('', { fetch }).textGeneration(input);
    assert.equal(exporter.getFinishedSpans().length, 2);
    await nextSdk.textGeneration(input, { fetch });
    assert.equal(exporter.getFinishedSpans().length, 3);
  } finally {
    instrumentation.disable();
    await provider.shutdown();
  }
});
