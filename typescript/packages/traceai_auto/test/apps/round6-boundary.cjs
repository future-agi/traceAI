const assert = require('node:assert/strict');
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
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
      // Use the application's SDK/peer closure, the same root inventoried by
      // auto. Loading through an instrumentor package can select a second ai
      // instance with a different zod peer resolution and bypass the app hook.
      const sdk = require('ai'), { MockLanguageModelV3 } = require('ai/test');
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
      const raw = app.filter(span => Object.prototype.hasOwnProperty.call(span.attributes, 'output.value'));
      assert.equal(raw.length, 1);
      const nativeStamp = span => JSON.stringify({ context: span.spanContext(), name: span.name, attributes: span.attributes, status: span.status, events: span.events, links: span.links, resource: span.resource.attributes, scope: span.instrumentationScope ?? span.instrumentationLibrary ?? {}, startTime: span.startTime, endTime: span.endTime, droppedAttributesCount: span.droppedAttributesCount, droppedEventsCount: span.droppedEventsCount, droppedLinksCount: span.droppedLinksCount });
      const nativeSpan = raw[0], ctx = nativeSpan.spanContext(), before = nativeStamp(nativeSpan);
      assert.equal(nativeSpan.attributes['gen_ai.response.model'], 'fixture-mode');
      assert.equal(nativeSpan.attributes['gen_ai.response.model'].length, 12);
      const matches = fi.filter(span => span.spanContext().traceId === ctx.traceId && span.spanContext().spanId === ctx.spanId);
      assert.equal(matches.length, 1);
      const clean = matches[0], unknown = '<unknown: host-truncated>';
      const enums = { 'fi.span.kind': ['LLM','CHAIN','TOOL','AGENT','RETRIEVER','EMBEDDING','RERANKER','GUARDRAIL','EVALUATOR'], 'gen_ai.span.kind': ['LLM','CHAIN','TOOL','AGENT','RETRIEVER','EMBEDDING','RERANKER','GUARDRAIL','EVALUATOR'], 'gen_ai.operation.name': ['chat','text_completion','embeddings','create_agent','invoke_agent','execute_tool','generate_content','retrieval','text_to_speech','speech_to_text'] };
      const counts = new Set(['gen_ai.usage.input_tokens','gen_ai.usage.output_tokens','gen_ai.usage.total_tokens','gen_ai.usage.cache_read_tokens','gen_ai.usage.cache_creation_tokens','gen_ai.usage.input_tokens.audio','gen_ai.usage.output_tokens.audio','gen_ai.usage.output_tokens.reasoning','traceai.capture.dropped_content_count']);
      const booleans = new Set(['traceai.wire.truncated','traceai.wire.incomplete','traceai.wire.body_unavailable','traceai.capture.host_data_unknown']);
      const nonContent = (key, value) => typeof value === 'string' && enums[key]?.includes(value) || typeof value === 'number' && Number.isSafeInteger(value) && (counts.has(key) && value >= 0 || key === 'http.response.status_code' && value >= 100 && value <= 599) || typeof value === 'boolean' && booleans.has(key);
      const check = attrs => { for (const [key, value] of Object.entries(attrs || {})) if (!nonContent(key, value)) assert.equal(value, key === 'session.id' ? '<redacted>' : unknown); };
      assert.equal(clean.name, unknown); assert.equal(clean.attributes['output.value'], unknown);
      assert.equal(clean.attributes['traceai.capture.host_data_unknown'], true);
      check(clean.attributes); check(clean.resource.attributes); check(clean.instrumentationScope.attributes);
      assert.equal(clean.instrumentationScope.name, unknown); assert.equal(clean.instrumentationScope.version, unknown); assert.equal(clean.instrumentationScope.schemaUrl, unknown);
      assert.deepEqual(clean.status, { code: nativeSpan.status.code, ...(nativeSpan.status.message === undefined ? {} : { message: unknown }) });
      assert.equal(clean.events.length, nativeSpan.events.length); assert.equal(clean.links.length, nativeSpan.links.length);
      for (let i = 0; i < clean.events.length; i++) { assert.equal(clean.events[i].name, unknown); check(clean.events[i].attributes); assert.deepEqual(clean.events[i].time, nativeSpan.events[i].time); assert.equal(clean.events[i].droppedAttributesCount, nativeSpan.events[i].droppedAttributesCount || 0); }
      for (let i = 0; i < clean.links.length; i++) { check(clean.links[i].attributes); assert.equal(clean.links[i].context.traceId, nativeSpan.links[i].context.traceId); assert.equal(clean.links[i].context.spanId, nativeSpan.links[i].context.spanId); assert.equal(clean.links[i].droppedAttributesCount, nativeSpan.links[i].droppedAttributesCount || 0); }
      for (const key of ['droppedAttributesCount','droppedEventsCount','droppedLinksCount']) assert.equal(clean[key], nativeSpan[key]);
      for (const key of ['startTime','endTime']) assert.deepEqual(clean[key], nativeSpan[key]);
      for (const [key, value] of [['gen_ai.usage.input_tokens',101],['gen_ai.usage.output_tokens',7],['gen_ai.usage.total_tokens',108]]) { assert.equal(nativeSpan.attributes[key], value); assert.equal(clean.attributes[key], value); }
      assert.equal(nativeStamp(nativeSpan), before);
      console.log(JSON.stringify({ protected: true, value: clean.attributes['output.value'], hostDataUnknown: clean.attributes['traceai.capture.host_data_unknown'], matchedByIdentity: true })); return;
    }
    const span = fi.find(s => (s.attributes['gen_ai.span.kind'] ?? s.attributes['fi.span.kind']) === 'LLM');
    if (!span) {
      let attributeError; try { require('@traceai/fi-core').privacyFilter().attributes(app[0]?.attributes); } catch (error) { attributeError = error.message; }
      assert(span, JSON.stringify({ fi: fi.map(s => [s.name, s.attributes]), app: app.map(s => s.attributes), attributeError }));
    }
    assert(!fi.some(s => s.attributes['traceai.capture.layer'] === 'wire'));
    const attrs = span.attributes; assert(!('traceai.attribute_projection.metadata' in attrs));
    assert.equal(attrs['gen_ai.usage.input_tokens'], 101); assert.equal(attrs['gen_ai.usage.output_tokens'], 7); assert.equal(attrs['gen_ai.usage.total_tokens'], 108);
    const extractedOutput = attrs['output.mime_type'] === 'application/json' || String(attrs['output.value']).startsWith('{') ? JSON.parse(attrs['output.value']).choices[0].message.content : attrs['output.value'];
    assert.equal(extractedOutput, 'answer');
    assert(Object.keys(span.attributes).length <= Number(process.env.LIMIT || 128));
    // Projection runs (and sets the content marker) only when the host limit is exceeded.
    const marker = attrs['traceai.capture.dropped_content_count'];
    if (span.droppedAttributesCount) { assert(marker > 0); assert(span.droppedAttributesCount >= marker); }
    else assert.equal(marker, undefined);
    console.log(JSON.stringify({ output: extractedOutput, input: attrs['gen_ai.usage.input_tokens'], total: attrs['gen_ai.usage.total_tokens'], dropped: span.droppedAttributesCount, marker }));
  } finally {
    await auto.shutdown(); if (selected) await selected.shutdown(); if (host) await host.shutdown();
    if (server) { server.closeAllConnections(); await new Promise(done => server.close(done)); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
