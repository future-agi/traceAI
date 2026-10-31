const assert = require('node:assert/strict');
const { trace, context } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const core = require('@traceai/fi-core');
const auto = process.env.VERIFY_SOURCE === '1' ? require('../../src/index.ts') : require('@traceai/auto');
const mode = process.env.MODE, app = [], fi = [];
const exporter = { export(spans, done) { fi.push(...spans); done({ code: 0 }); }, async shutdown() {} };
let host, supplied;
(async () => {
  try {
    if (mode.startsWith('host') || mode === 'ignored-half') {
      host = new BasicTracerProvider({ resource: resourceFromAttributes({ 'service.name': 'host-application', 'custom.identity': 'application-resource' }),
        spanLimits: { attributeCountLimit: Number(process.env.LIMIT || 128) }, spanProcessors: [new SimpleSpanProcessor({ export(spans, done) { app.push(...spans); done({ code: 0 }); }, async shutdown() {} })] });
      trace.setGlobalTracerProvider(host);
    }
    const options = { projectName: 'routing-project', instrumentations: ['wire'], configFile: '/does-not-exist/traceai-fi-config' };
    if (mode !== 'host-only' && mode !== 'processor-only') options.exporter = exporter;
    if (mode === 'host-only') options.provider = host;
    if (mode === 'processor-only') { supplied = new SimpleSpanProcessor(exporter); options.spanProcessor = supplied; }
    if (mode === 'ignored-half') options.apiKey = 'ignored-explicit-fi-api-key-private';
    const provider = auto.init(options);
    assert(provider); assert.equal(auto.report().active, true);
    if (mode !== 'ignored-half') { assert.equal(auto.report().config.apiKey, undefined); assert.equal(auto.report().config.secretKey, undefined); }
    const agentProcessors = provider._activeSpanProcessor?._spanProcessors ?? [];
    if (options.exporter) assert.equal(agentProcessors.at(-1).constructor.name, 'BatchSpanProcessor');
    if (mode === 'ignored-half') {
      const ignored = options.apiKey;
      assert(core.getRegisteredSecrets().includes(ignored));
      const span = provider.getTracer('app').startSpan('credential-echo', { attributes: { echoed: ignored } });
      span.setStatus({ code: 2, message: ignored }); span.addEvent(ignored, { echoed: ignored }); span.end();
      await auto.forceFlush();
      assert.equal(fi.length, 1); assert(!JSON.stringify(fi).includes(ignored)); assert.equal(app[0].attributes.echoed, ignored);
      assert(!JSON.stringify(auto.report()).includes(ignored));
      console.log(JSON.stringify({ ignoredRegistered: true, originalUnchanged: true, protected: true })); return;
    }
    if (mode === 'owned-projection' || mode === 'host-projection') {
      const body = JSON.stringify({ model: 'fixture-model', messages: Array.from({ length: 70 }, (_, i) => ({ role: 'user', content: `message ${i}` })) });
      const answer = JSON.stringify({ id: 'response-fixture', model: 'fixture-model', choices: [{ message: { role: 'assistant', content: 'answer' }, finish_reason: 'stop' }], usage: { prompt_tokens: 101, completion_tokens: 7, total_tokens: 108 } });
      const { createServer } = require('node:http');
      const server = createServer((req, res) => { req.resume(); req.on('end', () => { res.writeHead(200, { 'content-type': 'application/json' }); res.write(answer); }); });
      await new Promise(done => server.listen(0, '127.0.0.1', done));
      try {
        const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/chat/completions`, { method: 'POST', headers: { 'content-type': 'application/json' }, body });
        const reader = response.body.getReader(), item = await reader.read();
        assert.equal(Buffer.from(item.value).toString(), answer); await reader.cancel();
        await auto.forceFlush();
        const span = fi.find(s => s.attributes['traceai.capture.layer'] === 'wire'); assert(span);
        const attrs = span.attributes, recovered = { ...JSON.parse(attrs['traceai.attribute_projection.metadata'] || '{}'), ...attrs };
        assert.equal(attrs['output.value'], 'answer'); assert.equal(attrs['traceai.wire.incomplete'], true);
        assert.equal(recovered['gen_ai.usage.total_tokens'], 108); assert.equal(recovered['gen_ai.response.id'], 'response-fixture');
        assert(Object.keys(attrs).length <= Number(process.env.LIMIT || 128));
        assert(span.droppedAttributesCount > 0); assert.equal(span.droppedAttributesCount, attrs['traceai.attribute_projection.dropped_count']);
        console.log(JSON.stringify({ projected: true, output: attrs['output.value'], incomplete: attrs['traceai.wire.incomplete'], dropped: span.droppedAttributesCount, count: Object.keys(attrs).length, service: span.resource.attributes['service.name'] }));
      } finally { server.closeAllConnections(); await new Promise(done => server.close(done)); }
      return;
    }
    provider.getTracer('application').startSpan('neutral-span', { attributes: { safe: 'visible' } }).end();
    await auto.forceFlush();
    const spans = mode === 'host-only' ? app : fi;
    assert.equal(spans.length, 1); assert.equal(spans[0].attributes.safe, 'visible');
    console.log(JSON.stringify({ active: true, received: true, processors: agentProcessors.map(p => p.constructor.name), service: spans[0].resource.attributes['service.name'], custom: spans[0].resource.attributes['custom.identity'] }));
  } finally { await auto.shutdown(); if (host) await host.shutdown(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
