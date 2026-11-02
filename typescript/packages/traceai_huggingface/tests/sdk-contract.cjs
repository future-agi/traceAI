const assert = require('node:assert/strict');
const { test } = require('node:test');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { HuggingFaceInstrumentation } = require('../dist/cjs');

// Real SDKs, synthetic responses only. The 2.0.0 SDK uses global fetch.
for (const sdkName of ['@huggingface/inference-floor', '@huggingface/inference', '@huggingface/inference-current']) {
  test(`${sdkName}: real client emits a text generation span`, async () => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new HuggingFaceInstrumentation();
    instrumentation.setTracerProvider(provider);
    const originalFetch = global.fetch;
    let calls = 0;
    const fakeFetch = async () => {
      calls++;
      return new Response(JSON.stringify(sdkName.endsWith('-current') ? { choices: [{ text: 'offline answer' }] } : [{ generated_text: 'offline answer' }]), { headers: { 'content-type': 'application/json' } });
    };
    global.fetch = fakeFetch;
    try {
      const sdk = require(sdkName);
      const wrapped = instrumentation.manuallyInstrument(sdk) || sdk;
      const Client = wrapped.InferenceClient || wrapped.HfInference;
      const client = new Client('', { fetch: fakeFetch });
      const result = await client.textGeneration({ model: 'offline-model', endpointUrl: 'http://localhost:9999', inputs: 'offline prompt' });
      assert.equal(result.generated_text, 'offline answer');
      assert.equal(calls, 1);
      assert.equal(exporter.getFinishedSpans().length, 1, 'successful SDK call must produce exactly one span');
    } finally {
      global.fetch = originalFetch;
      instrumentation.disable();
      await provider.shutdown();
    }
  });
}
