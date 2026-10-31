const assert = require('node:assert/strict');
const { MistralInstrumentation } = require('../../dist/src');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const instrumentation = new MistralInstrumentation();
instrumentation.setTracerProvider(provider);
// Import after instrumentation: exercises the actual require-in-the-middle hook.
const { Mistral } = require('@mistralai/mistralai');
const client = new Mistral({ apiKey: 'synthetic', httpClient: { request: async () => new Response(JSON.stringify({
  id: 'test', object: 'chat.completion', created: 1, model: 'test',
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  choices: [{ index: 0, message: { role: 'assistant', content: 'hello' }, finish_reason: 'stop' }],
}), { headers: { 'content-type': 'application/json' } }) } });
(async () => {
  await client.chat.complete({ model: 'test', messages: [{ role: 'user', content: 'hello' }] });
  assert.equal(exporter.getFinishedSpans().length, 1);
  instrumentation.disable();
  await client.chat.complete({ model: 'test', messages: [{ role: 'user', content: 'hello' }] });
  assert.equal(exporter.getFinishedSpans().length, 1);
  instrumentation.enable();
  await client.chat.complete({ model: 'test', messages: [{ role: 'user', content: 'hello' }] });
  assert.equal(exporter.getFinishedSpans().length, 2);
  instrumentation.disable();
  await provider.shutdown();
})().catch(error => { console.error(error); process.exitCode = 1; });
