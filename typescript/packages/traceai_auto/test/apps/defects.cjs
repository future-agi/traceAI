const assert = require('node:assert/strict');
const { trace, context } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const mode = process.env.MODE;
const appSpans = [], fiSpans = [];
const exporter = { export(spans, done) { fiSpans.push(...spans); done({ code: 0 }); }, async shutdown() {} };
const appExporter = { export(spans, done) { appSpans.push(...spans); done({ code: 0 }); }, async shutdown() {} };
const options = { apiKey: 'configured-PRIVATE-123', secretKey: 'secret-PRIVATE-123', instrumentations: ['wire'], exporter };
function installProvider() {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(appExporter)] });
  trace.setGlobalTracerProvider(provider);
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  return provider;
}
function serialized(span) { return JSON.stringify({ name: span.name, attributes: span.attributes, status: span.status, events: span.events }); }
(async () => {
  if (mode === 'queued-signals') {
    const { chainSigterm } = require('../../dist/src/signals.js');
    let release, entered, flushes = 0, calls = 0;
    const started = new Promise(resolve => { entered = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    const keepalive = setInterval(() => {}, 1000);
    process.once('SIGTERM', () => { calls++; process.send({ type: 'handler', calls }); });
    chainSigterm(async () => { flushes++; if (flushes === 1) { entered(); process.send({ type: 'flushing' }); await gate; } else process.send({ type: 'second-flush', flushes, calls }); });
    // Acknowledge actual native receipt, not just kill() returning in the parent.
    const gated = process.emit;
    let deliveries = 0;
    process.emit = function(...args) {
      const result = Reflect.apply(gated, this, args);
      if (args[0] === 'SIGTERM' && ++deliveries === 2) process.send({ type: 'queued' });
      return result;
    };
    // Rebind the native dispatch so the receipt observer above sees both OS signals.
    const events = process._events, saved = Object.getOwnPropertyDescriptor(events, 'SIGTERM');
    const start = process.rawListeners('newListener').find(fn => fn.name === 'startListeningIfSignal');
    const stop = process.rawListeners('removeListener').find(fn => fn.name === 'stopListeningIfSignal');
    Object.defineProperty(events, 'SIGTERM', { ...saved, value: undefined }); Reflect.apply(stop, process, ['SIGTERM']);
    Object.defineProperty(events, 'SIGTERM', saved); Reflect.apply(start, process, ['SIGTERM']);
    process.on('message', message => { if (message === 'release') release(); });
    process.send({ type: 'ready' });
    await started; return;
  }
  if (mode === 'legacy-real') {
    // Pin the real 1.x SDK independently of production's 2.x SDK, never a mock.
    let legacyPath;
    try { legacyPath = require.resolve('otel-sdk-trace-base-legacy'); }
    catch {
      // --lockfile-only leaves pre-existing local links alone. Use the identical
      // pinned SDK already in pnpm's workspace store until the next full install.
      const { readdirSync } = require('node:fs'), { resolve, join } = require('node:path');
      const store = resolve(__dirname, '../../../../node_modules/.pnpm');
      const directory = readdirSync(store).find(name => name.startsWith('@opentelemetry+sdk-trace-base@1.30.1_'));
      assert(directory, 'Real OTel 1.30.1 SDK must be installed');
      legacyPath = join(store, directory, 'node_modules/@opentelemetry/sdk-trace-base');
    }
    const legacy = require(legacyPath);
    const provider = new legacy.BasicTracerProvider(); provider.addSpanProcessor(new legacy.SimpleSpanProcessor(appExporter));
    trace.setGlobalTracerProvider(provider); context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
    const auto = require('@traceai/auto'); auto.init({ ...options, exporter: undefined, instrumentations: [] });
    const tracer = provider.getTracer('legacy-app', '1.2.3');
    const parent = tracer.startSpan('legacy-parent');
    const child = tracer.startSpan('legacy-child', {}, trace.setSpan(context.active(), parent)); child.end(); parent.end();
    await auto.forceFlush();
    assert.equal(trace.getTracerProvider().getDelegate(), provider); assert.equal(appSpans.length, 2);
    assert.equal(appSpans[0].instrumentationScope, undefined); assert.equal(appSpans[0].parentSpanId, parent.spanContext().spanId);
    assert.equal(auto.report().collector, 'accepted');
    await auto.shutdown(); console.log(JSON.stringify({ sameProvider: true, appSpans: appSpans.length, collector: auto.report().collector })); return;
  }
  const provider = installProvider();
  if (mode.startsWith('loader-')) {
    const auto = require('@traceai/auto'), descriptor = Object.getOwnPropertyDescriptor(globalThis, 'fetch');
    const attempts = mode === 'loader-retry-twice' ? 2 : mode === 'loader-retry' ? 1 : 0;
    const failed = [];
    try {
      for (let i = 0; i < attempts; i++) {
        Object.defineProperty(globalThis, 'fetch', { configurable: true, get() { return descriptor.value; }, set() { throw new Error('fetch installation failed'); } });
        auto.init({ ...options, instrumentations: i ? ['http', 'wire'] : ['openai', 'wire'] }); failed.push(auto.report().active);
        Object.defineProperty(globalThis, 'fetch', descriptor);
      }
      auto.init({ ...options, instrumentations: ['http'] }); const result = auto.report();
      // This module's HTTP builtin is imported only after the synchronous retry.
      await (await import('./retry-http.mjs')).request();
      await auto.forceFlush();
      assert.equal(result.active, attempts === 0); assert(failed.every(active => active === false));
      assert.equal(fiSpans.filter(span => span.kind === 1).length, attempts ? 0 : 1);
      assert.equal(appSpans.filter(span => span.kind === 1).length, attempts ? 0 : 1);
      console.log(JSON.stringify({ failed, active: result.active, serverSpans: fiSpans.filter(span => span.kind === 1).length, status: result.integrations.find(i => i.name === 'http')?.status ?? 'not_enabled' }));
    } finally { Object.defineProperty(globalThis, 'fetch', descriptor); await auto.shutdown(); }
    return;
  }
  if (mode === 'fetch-ownership') {
    let receiver, args;
    const response = new Response('{"choices":[{"message":{"content":"answer"}}]}', { headers: { 'content-type': 'application/json' } });
    const promise = Promise.resolve(response);
    globalThis.fetch = function(...values) { receiver = this; args = values; return promise; };
    const auto = require('@traceai/auto'); auto.init(options);
    const inner = globalThis.fetch;
    const outer = function(...values) { return Reflect.apply(inner, this, values); }; globalThis.fetch = outer;
    await auto.shutdown();
    const owner = {}, input = new URL('https://api.openai.com/v1/chat/completions'), init = { method: 'POST', body: '{"messages":[]}' };
    const result = Reflect.apply(globalThis.fetch, owner, [input, init]);
    assert.equal(result, promise); assert.equal(receiver, owner); assert.equal(args[0], input); assert.equal(args[1], init);
    assert.equal(await result, response); await response.text(); await provider.forceFlush();
    assert.equal(globalThis.fetch, outer); assert.equal(appSpans.length, 0); assert.equal(fiSpans.length, 0);
    console.log(JSON.stringify({ outerPreserved: true, appSpans: 0, fiSpans: 0, identityPreserved: true, active: auto.report().active })); return;
  }
  const { createServer } = require('node:http');
  const credential = 'CUSTOM-REQUEST-PRIVATE-123', bytes = 'U0VDUkVULUFVRElP';
  let requests = 0, sent;
  const isError = mode !== 'library-ownership';
  const payload = mode === 'library-audio' ? { error: { audio: { format: 'wav', source: { bytes } } } } : mode === 'library-bracket-audio' ? { error: { message: `invalid payload [request: ${JSON.stringify({ audio: { format: 'wav', source: { bytes } } })}]` } } : { error: { message: `Invalid upstream key ${credential}` } };
  const completion = { id: 'test', model: 'm', choices: [{ message: { role: 'assistant', content: 'answer' }, finish_reason: 'stop' }] };
  const server = createServer((req, res) => { req.resume(); req.on('end', () => { requests++; sent = req.headers['x-api-key']; res.writeHead(isError ? 400 : 200, { 'content-type': 'application/json' }); res.end(mode === 'library-truncated-audio' ? '{"audio":{"source":{"bytes":"' + bytes + '"}}' : JSON.stringify(isError ? payload : completion)); }); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const auto = require('@traceai/auto'); auto.init({ ...options, instrumentations: ['openai', 'wire'] });
  const OpenAI = require('openai'), client = new OpenAI({ apiKey: 'sdk-key-PRIVATE-123', baseURL: `http://127.0.0.1:${server.address().port}/v1`, maxRetries: 0 });
  try {
    if (mode === 'library-ownership') {
      const target = OpenAI.Chat.Completions.prototype, inner = target.create;
      let calls = 0;
      const outer = function(...args) { calls++; return Reflect.apply(inner, this, args); }; target.create = outer;
      const call = () => client.chat.completions.create({ model: 'm', messages: [] });
      assert.equal((await call()).choices[0].message.content, 'answer'); await auto.forceFlush();
      assert.equal(appSpans.length, 1); assert.equal(fiSpans.length, 1);
      await auto.shutdown();
      assert.equal((await call()).choices[0].message.content, 'answer'); await provider.forceFlush();
      assert.equal(target.create, outer); assert.equal(appSpans.length, 1); assert.equal(fiSpans.length, 1); assert.equal(calls, 2); assert.equal(requests, 2);
      console.log(JSON.stringify({ outerPreserved: true, appSpans: 1, fiSpans: 1, calls, requests, active: auto.report().active }));
    } else {
      let failure;
      try { await client.chat.completions.create({ model: 'm', messages: [] }, { headers: { [process.env.HEADER_NAME || 'X-API-Key']: credential } }); }
      catch (error) { failure = error; }
      assert(failure); assert.equal(failure.status, 400); await auto.forceFlush();
      assert.equal(sent, credential); assert.equal(fiSpans.length, 1); assert.equal(appSpans.length, 1);
      const forbidden = ['library-audio', 'library-bracket-audio', 'library-truncated-audio'].includes(mode) ? bytes : credential;
      // Q7b, 2026-11-05: the application Error is unchanged; FI-authored status is protected.
      assert(failure.message.includes(forbidden));
      const beforeError = Object.getOwnPropertyDescriptors(failure);
      assert(!appSpans[0].status.message.includes(forbidden));
      if(forbidden === credential) assert(appSpans[0].status.message.includes('<redacted>'));
      await auto.forceFlush();
      assert.deepEqual(Object.getOwnPropertyDescriptors(failure),beforeError);
      assert(!serialized(fiSpans[0]).includes(forbidden)); assert.equal(fiSpans[0].status.code, 2);
      assert(fiSpans[0].events.some(event => event.name === 'exception'));
      await auto.shutdown(); console.log(JSON.stringify({ protected: true, applicationUnchanged: true, exported: fiSpans.length, requests }));
    }
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
