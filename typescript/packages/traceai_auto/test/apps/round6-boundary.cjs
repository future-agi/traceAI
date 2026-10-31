const assert = require('node:assert/strict');
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const auto = process.env.VERIFY_SOURCE === '1' ? require('../../src/index.ts') : require('@traceai/auto');
const mode = process.env.MODE, app = [], fi = [];
const exporter = { export(spans, done) { fi.push(...spans); done({ code: 0 }); }, async shutdown() {} };
const native = { export(spans, done) { app.push(...spans); done({ code: 0 }); }, async shutdown() {} };
let host, selected, server;
(async () => {
  try {
    const options = { exporter, configFile: '/does-not-exist/traceai-fi-config', instrumentations: ['wire'] };
    if (mode === 'library-limit' || mode === 'wire-value-limit' || mode === 'vercel-provider') {
      host = new BasicTracerProvider({ spanLimits: { attributeCountLimit: Number(process.env.LIMIT || 128), ...(mode === 'wire-value-limit' ? { attributeValueLengthLimit: 12 } : {}) }, spanProcessors: [new SimpleSpanProcessor(native)] });
      trace.setGlobalTracerProvider(host);
    }
    if (mode === 'library-limit') options.instrumentations = ['wire', 'openai'];
    if (mode === 'neutral-genkit') options.instrumentations = ['genkit'];
    if (mode === 'neutral-mastra') options.instrumentations = ['mastra'];
    if (mode === 'vercel-provider') {
      options.instrumentations = ['vercel'];
      selected = new BasicTracerProvider(); options.provider = selected;
    }
    const secret = 'k9V7h2qR4p8Z_SYNTHETIC_FI_CREDENTIAL';
    if (mode === 'wire-value-limit') { options.apiKey = secret; options.secretKey = secret + '_SECRET'; }
    const provider = auto.init(options); assert(provider); assert(auto.report().active);
    if (mode === 'neutral-mastra') {
      assert.equal(auto.report().config.apiKey, undefined); assert.equal(auto.report().config.secretKey, undefined);
      console.log(JSON.stringify({ active: true, status: auto.report().integrations.find(s => s.name === 'mastra')?.status })); return;
    }
    if (mode === 'neutral-genkit') {
      const status = auto.report().integrations.find(s => s.name === 'genkit'); assert.equal(status.status, 'instrumented', JSON.stringify(status));
      provider.getTracer('genkit-tracer').startSpan('model', { attributes: { 'genkit:type': 'action', 'genkit:metadata:subtype': 'model', 'genkit:name': 'fixture/model', 'genkit:output': JSON.stringify({ message: { role: 'model', content: [{ text: 'answer' }] } }) } }).end();
      await auto.forceFlush(); assert(fi.some(s => s.attributes['gen_ai.span.kind'] === 'LLM'));
      assert.equal(auto.report().config.apiKey, undefined); assert.equal(auto.report().config.secretKey, undefined);
      console.log(JSON.stringify({ active: true, status: status.status, canonical: true })); return;
    }
    if (mode === 'vercel-provider') {
      const appRequire = createRequire(resolve(__dirname, '../../../traceai_vercel/package.json'));
      const sdk = appRequire('ai'), { MockLanguageModelV3 } = appRequire('ai/test');
      const result = await sdk.generateText({ prompt: 'hello', model: new MockLanguageModelV3({ doGenerate: {
        content: [{ type: 'text', text: 'answer' }], warnings: [], finishReason: { unified: 'stop', raw: 'stop' },
        usage: { inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } },
      } }) });
      assert.equal(result.text, 'answer'); await auto.forceFlush();
      assert(fi.length > 0); assert.equal(app.length, 0); assert(fi.some(s => s.attributes['gen_ai.span.kind'] === 'LLM'));
      console.log(JSON.stringify({ result: result.text, selectedSpans: fi.length, globalSpans: app.length })); return;
    }
    const { createServer } = require('node:http');
    const output = mode === 'wire-value-limit' ? secret : 'answer';
    server = createServer((req, res) => { req.resume(); req.on('end', () => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ id: 'response-fixture', model: 'fixture-model', choices: [{ message: { role: 'assistant', content: output }, finish_reason: 'stop' }], usage: { prompt_tokens: 101, completion_tokens: 7, total_tokens: 108 } })); }); });
    await new Promise(done => server.listen(0, '127.0.0.1', done));
    const baseURL = `http://127.0.0.1:${server.address().port}/v1`;
    const messages = Array.from({ length: 70 }, (_, i) => ({ role: 'user', content: `message ${i}` }));
    if (mode === 'library-limit') {
      const OpenAI = require('openai');
      const client = new OpenAI({ apiKey: 'synthetic-provider-credential', baseURL, fetch: globalThis.fetch });
      const result = await client.chat.completions.create({ model: 'fixture-model', messages }); assert.equal(result.choices[0].message.content, 'answer');
    } else {
      const result = await fetch(baseURL + '/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'fixture-model', messages }) });
      assert.equal((await result.json()).choices[0].message.content, secret);
      const { FITracer } = require('@traceai/fi-core');
      const linkContext = { traceId: '1'.repeat(32), spanId: '2'.repeat(16), traceFlags: 1 };
      const span = new FITracer({ tracer: provider.getTracer('sdk-value-limits') }).startSpan('sdk-value-limits', { links: [{ context: linkContext, attributes: { echoed: secret } }] });
      span.addEvent('event', { echoed: secret }); span.recordException(new Error(secret));
      span.addLink({ context: linkContext, attributes: { echoed: secret } }); span.end();
    }
    await auto.forceFlush();
    if (mode === 'wire-value-limit') {
      assert(fi.length > 0); assert(!JSON.stringify(fi).includes(secret.slice(0, 12)));
      console.log(JSON.stringify({ protected: true, value: fi[0].attributes['output.value'] })); return;
    }
    const span = fi.find(s => (s.attributes['gen_ai.span.kind'] ?? s.attributes['fi.span.kind']) === 'LLM');
    if (!span) {
      let attributeError; try { require('@traceai/fi-core').privacyFilter().attributes(app[0]?.attributes); } catch (error) { attributeError = error.message; }
      assert(span, JSON.stringify({ fi: fi.map(s => [s.name, s.attributes]), app: app.map(s => s.attributes), attributeError }));
    }
    assert(!fi.some(s => s.attributes['traceai.capture.layer'] === 'wire'));
    const attrs = { ...JSON.parse(span.attributes['traceai.attribute_projection.metadata'] || '{}'), ...span.attributes };
    assert.equal(attrs['gen_ai.usage.input_tokens'], 101); assert.equal(attrs['gen_ai.usage.output_tokens'], 7); assert.equal(attrs['gen_ai.usage.total_tokens'], 108);
    const extractedOutput = attrs['output.mime_type'] === 'application/json' || String(attrs['output.value']).startsWith('{') ? JSON.parse(attrs['output.value']).choices[0].message.content : attrs['output.value'];
    assert.equal(extractedOutput, 'answer');
    assert(Object.keys(span.attributes).length <= Number(process.env.LIMIT || 128));
    if (span.droppedAttributesCount) assert.equal(span.droppedAttributesCount, attrs['traceai.attribute_projection.dropped_count']);
    console.log(JSON.stringify({ output: extractedOutput, input: attrs['gen_ai.usage.input_tokens'], total: attrs['gen_ai.usage.total_tokens'], dropped: span.droppedAttributesCount, marker: attrs['traceai.attribute_projection.dropped_count'] }));
  } finally {
    await auto.shutdown(); if (selected) await selected.shutdown(); if (host) await host.shutdown();
    if (server) { server.closeAllConnections(); await new Promise(done => server.close(done)); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
