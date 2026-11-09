const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { trace, context } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const mode = process.env.MODE, agent = process.env.AGENT === 'true', app = [], fi = [], collected = [];
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor({ export(spans, done) { app.push(...spans); done({ code: 0 }); }, async shutdown() {} })] });
trace.setGlobalTracerProvider(provider); context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const core = require('@traceai/fi-core'), secret = mode.endsWith('long-query') ? 'query-private-' + 'Q'.repeat(8203) : 'runtime-provider-private-token-' + mode;
let instrumentor;
if (mode === 'openai-preinstalled') {
  const { OpenAIInstrumentation } = require('@traceai/openai');
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  instrumentor = new OpenAIInstrumentation(); registerInstrumentations({ tracerProvider: provider, instrumentations: [instrumentor] });
}
let OpenAI = mode === 'openai-preinstalled' ? require('openai') : undefined;
const wrapper = OpenAI?.Chat?.Completions?.prototype.create;
const auto = require('@traceai/auto');
const server = createServer((request, response) => {
  let body = ''; request.on('data', chunk => { body += chunk; });
  request.on('end', () => {
    if (request.url === '/tracer/v1/traces') { collected.push(JSON.parse(body)); response.end('{}'); return; }
    response.writeHead(401, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ error: { message: `Invalid credential ${secret}` }, receivedBytes: Buffer.byteLength(body) }));
  });
});
(async () => {
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const local = `http://127.0.0.1:${server.address().port}`;
  try {
    if (mode === 'copies') {
      // The embedded agent owns the exporter; app-installed ESM/CJS and a second
      // independently compiled fi-core copy must register into its same registry.
      Object.assign(process.env, { FI_API_KEY: 'embedded-fi-api-private-token', FI_SECRET_KEY: 'embedded-fi-secret-private-token', FI_BASE_URL: local, FI_INSTRUMENTATIONS: 'wire', OTEL_SDK_DISABLED: 'false' });
      require('../../dist/agent/preload.cjs');
      const esm = await import('@traceai/auto');
      const esmCore = await import('@traceai/fi-core');
      const Module = require('node:module'), fs = require('node:fs'), path = require('node:path');
      const source = path.join(path.dirname(require.resolve('@traceai/fi-core')), 'secrets.js'), copy = new Module(source + '-independent-copy');
      copy._compile(fs.readFileSync(source, 'utf8'), source + '-independent-copy');
      const tokens = ['from-auto-cjs-private-token', 'from-auto-esm-private-token', 'from-core-esm-private-token', 'from-independent-copy-private-token', 'synthetic-environment-credential'];
      auto.registerSecret(tokens[0]); esm.registerSecret(tokens[1]); esmCore.registerSecret(tokens[2]); copy.exports.registerSecret(tokens[3]);
      const holder = globalThis[Symbol.for('traceai.secrets')];
      const span = provider.getTracer('copies').startSpan('app', { attributes: { echoed: tokens.join(' ') } }); span.end(); await auto.forceFlush();
      assert(collected.length > 0); const output = JSON.stringify(collected);
      assert(tokens.every(value => !output.includes(value))); assert(tokens.every(value => holder.values().includes(value)));
      console.log(JSON.stringify({ shared: true, exported: typeof esm.registerSecret === 'function' && typeof core.registerSecret === 'function', protected: true })); return;
    }
    if (mode === 'structural-media') {
      if (agent) auto.init({ apiKey: 'configured-fi-private-token', secretKey: 'configured-fi-secret-private-token', exporter: { export(spans, done) { fi.push(...spans); done({ code: 0 }); }, async shutdown() {} }, instrumentations: ['wire'] });
      const bytes = 'U0VDUkVU', cases = [
        ['indexed-audio', { 'input.audio.0': bytes }],
        ['mime-audio', { 'input.parts.0.inlineData.data': bytes, 'input.parts.0.inlineData.mimeType': 'audio/wav' }],
        ['indexed-mime-audio', { 'input.parts.0.inlineData.data.0': bytes, 'input.parts.0.inlineData.mimeType': 'audio/wav' }],
        ['indexed-mime-audio-array', { 'input.parts.0.inlineData.data.0': [bytes, bytes], 'input.parts.0.inlineData.mimeType': 'audio/wav' }],
        ...['audio', 'input_audio'].flatMap(key => [false, true].map(array => [`json-${key}-${array ? 'array' : 'scalar'}`, { 'input.value': JSON.stringify({ [key]: array ? [bytes] : bytes }) }])),
        ['indexed-image', { 'input.images.0': bytes.repeat(40) }],
        ['data-uri', { 'input.value': 'data:image/svg+xml,%3Csvg%3Eprivate-image%3C%2Fsvg%3E' }],
      ];
      const tracer = provider.getTracer('application');
      for (const [name, attributes] of cases) {
        const span = tracer.startSpan(name, { attributes: { ...attributes, safe: 'ordinary application text' } });
        span.addEvent('payload', attributes); span.end();
      }
      if (agent) await auto.forceFlush();
      const application = app.map(span => ({ name: span.name, attributes: span.attributes, events: span.events.map(event => ({ name: event.name, attributes: event.attributes })) }));
      const output = JSON.stringify(fi.map(span => ({ attributes: span.attributes, events: span.events })));
      console.log(JSON.stringify({ application, fiSpans: fi.length, protected: agent && !output.includes(bytes) && !output.includes('private-image'), appUnchanged: application.every((span, i) => Object.entries(cases[i][1]).every(([key, value]) => JSON.stringify(span.attributes[key]) === JSON.stringify(value))), safeRetained: agent && fi.every(span => span.attributes.safe === 'ordinary application text') })); return;
    }
    if (agent && mode.startsWith('openai')) auto.init({ apiKey: 'configured-fi-private-token', secretKey: 'configured-fi-secret-private-token', exporter: { export(spans, done) { fi.push(...spans); done({ code: 0 }); }, async shutdown() {} }, instrumentations: mode.startsWith('openai') ? ['openai', 'wire'] : ['wire'] });
    if (mode.startsWith('openai')) {
      OpenAI ??= require('openai');
      const afterWrapper = OpenAI.Chat.Completions.prototype.create;
      const client = new OpenAI({ apiKey: secret, baseURL: local + '/v1', maxRetries: 0 });
      let application;
      try { await client.chat.completions.create({ model: 'fixture', messages: [] }); assert.fail('expected 401'); }
      catch (error) { assert.equal(error.status, 401); application = { status: error.status, message: error.message, name: error.name }; assert(error.message.includes(secret)); }
      const unrelated = provider.getTracer('application').startSpan('unrelated', { attributes: { echoed: secret } }); unrelated.setStatus({ code: 2, message: secret }); unrelated.end();
      if (agent) {
        await auto.forceFlush(); assert.equal(fi.length, 2);
        const output = JSON.stringify(fi.map(span => ({ name: span.name, attributes: span.attributes, status: span.status, events: span.events })));
        assert(!output.includes(secret)); assert(app.some(span => span.status.message?.includes(secret)));
        assert(core.getRegisteredSecrets().includes(secret));
      }
      console.log(JSON.stringify({ application, sameWrapper: mode === 'openai-preinstalled' ? wrapper === afterWrapper : true, fiSpans: fi.length, wireSpans: fi.filter(span => span.attributes['traceai.capture.layer'] === 'wire').length, protected: agent, appUnchanged: app.some(span => span.status.message?.includes(secret)) })); return;
    }
    if (mode.startsWith('early-body-') || mode.endsWith('long-query')) {
      if (agent) auto.init({ apiKey: 'configured-fi-private-token', secretKey: 'configured-fi-secret-private-token', exporter: { export(spans, done) { fi.push(...spans); done({ code: 0 }); }, async shutdown() {} }, instrumentations: ['wire'], ...(mode === 'early-body-truncated' ? { wireMaxBodyBytes: 64 } : {}) });
      let response, read;
      if (mode.endsWith('long-query')) {
        const url = local + '/plain?token=' + encodeURIComponent(secret);
        if (mode === 'http-long-query') {
          response = await new Promise((done, reject) => require('node:http').get(url, done).on('error', reject));
          read = () => new Promise((done, reject) => { let text = ''; response.setEncoding('utf8'); response.on('data', chunk => { text += chunk; }); response.on('end', () => done(text)); response.on('error', reject); });
        } else if (mode === 'undici-request-long-query') {
          response = await require('undici').request(url); read = () => response.body.text();
        } else {
          response = await (mode === 'undici-long-query' ? require('undici').fetch : fetch)(url); read = () => response.text();
        }
      } else {
        let body;
        if (mode === 'early-body-truncated') body = JSON.stringify({ messages: [], mcp: { authorization_token: secret } });
        else {
          body = new FormData(); body.set('model', 'whisper-1');
          body.append('settings', JSON.stringify({ padding: 'x'.repeat(mode === 'early-body-cropped' ? 65536 : 0), mcp: { authorization_token: secret } }));
          if (mode === 'early-body-duplicate') body.append('settings', 'ordinary settings');
        }
        response = await fetch(local + (mode === 'early-body-truncated' ? '/v1/chat/completions' : '/v1/audio/transcriptions'), { method: 'POST', body });
        read = () => response.text();
      }
      // Export independently of wire completion; leave this response entirely unread.
      const unrelated = provider.getTracer('application').startSpan('before-response-consumption', { attributes: { echoed: secret, safe: 'ordinary application text' } });
      unrelated.setStatus({ code: 2, message: 'Invalid ' + secret }); unrelated.end();
      if (agent) await auto.forceFlush();
      const beforeKnown = core.getRegisteredSecrets().includes(secret), beforeCount = fi.length;
      const beforeProtected = !JSON.stringify(fi.map(span => ({ attributes: span.attributes, status: span.status }))).includes(secret);
      const application = { status: response.status ?? response.statusCode, body: await read() };
      assert(application.body.includes(secret));
      if (agent) await auto.forceFlush();
      console.log(JSON.stringify({ application, beforeKnown, beforeCount, beforeProtected, appUnchanged: app.find(span => span.name === 'before-response-consumption')?.attributes.echoed === secret, safeRetained: fi[0]?.attributes.safe === 'ordinary application text', wire: fi.filter(span => span.attributes['traceai.capture.layer'] === 'wire').length })); return;
    }
    // Opaque Request/stream bodies are intentionally never cloned, buffered or parsed.
    const native = globalThis.fetch, body = JSON.stringify({ messages: [], mcp_servers: [{ authorization_token: secret }] });
    // Install this loopback-only proxy before the hook so the catalog-provider URL
    // stays visible to capture. The real native transport never contacts the provider.
    const { createFetchHook } = require('../../dist/src/wire/fetch-runtime');
    const { observeOutgoing } = require('../../dist/src/wire/observe-node');
    const actualFetch = globalThis.fetch;
    globalThis.fetch = (input, init) => actualFetch(local + '/v1/chat/completions', init ?? { method: input.method, body: input.body, headers: input.headers, duplex: 'half' });
    const results = [], hook = agent ? createFetchHook(4096, result => results.push(result), { active: () => null, skip: () => false, run: (_p, work) => work(), observeRequest: observeOutgoing }) : undefined;
    try {
      const stream = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(body)); c.close(); } });
      const init = { method: 'POST', body: stream, duplex: 'half', headers: { 'content-type': 'application/json' } };
      const response = mode === 'request' ? await fetch(new Request('https://api.openai.com/v1/chat/completions', init)) : await fetch('https://api.openai.com/v1/chat/completions', init);
      const application = { status: response.status, body: await response.text() };
      assert(application.body.includes(secret));
      console.log(JSON.stringify({ application, unparsed: !core.getRegisteredSecrets().includes(secret), bodyUnavailable: agent && results[0]?.attributes['traceai.wire.body_unavailable'] === true }));
    } finally { hook?.remove(); globalThis.fetch = native; }
  } finally { if (agent) await auto.shutdown(); instrumentor?.disable(); server.closeAllConnections(); await new Promise(done => server.close(done)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
