const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { PortkeyInstrumentation } = require('../../../dist/src');
const { InMemorySpanExporter, SimpleSpanProcessor, BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const inst = new PortkeyInstrumentation();
inst.setTracerProvider(provider);
const request = createRequire(process.argv[2]);
const sdk = request('portkey-ai');
globalThis.fetch = async () => new Response(JSON.stringify({
  id: 'synthetic', model: 'synthetic-model',
  choices: [{ message: { role: 'assistant', content: 'hello' } }],
  usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
}), { headers: { 'content-type': 'application/json' } });
(async () => {
  try {
    class CustomClient extends sdk.Portkey {}
    const client = new CustomClient({ apiKey: 'synthetic-key', baseURL: 'http://localhost:9999' });
    assert(client instanceof sdk.Portkey);
    assert(client instanceof CustomClient);
    assert.equal(sdk.Portkey, sdk.default);
    await client.chat.completions.create({ model: 'synthetic-model', messages: [] });
    assert.equal(exporter.getFinishedSpans().length, 1);
    assert.equal(exporter.getFinishedSpans()[0].attributes['gen_ai.provider.name'], 'portkey');
    inst.disable();
    await client.chat.completions.create({ model: 'synthetic-model', messages: [] });
    assert.equal(exporter.getFinishedSpans().length, 1);
    inst.enable();
    await client.chat.completions.create({ model: 'synthetic-model', messages: [] });
    assert.equal(exporter.getFinishedSpans().length, 2);
    console.log('automatic tracing passed');
  } finally { inst.disable(); await provider.shutdown(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
