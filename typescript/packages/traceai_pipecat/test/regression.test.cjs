const { test } = require('node:test');
const assert = require('node:assert/strict');
const { InMemorySpanExporter, SimpleSpanProcessor, BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
const { PipecatInstrumentation } = require('../dist/src/index.js');
const { createClient } = require('./transport.cjs');

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`real ${name}: connect exports a completed client span`, async () => {
    const sdk = require(name);
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new PipecatInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
    const { client } = createClient(sdk);
    try {
      await client.connect();
      assert.equal(exporter.getFinishedSpans().filter(s => s.attributes['pipecat.operation'] === 'client.connect').length, 1);
    } finally {
      await client.disconnect();
      instrumentation.disable();
      await provider.shutdown();
    }
  });
}
