// Exercise the built instrumentor, real FITracer, real SDK and OTLP/HTTP exporter.
// OLLAMA_SDK_PATH selects an isolated SDK install for the compatibility matrix.
const assert = require('node:assert/strict');
const { test, before, after, beforeEach, afterEach } = require('node:test');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { createServer } = require('node:http');
const { once } = require('node:events');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { BasicTracerProvider, SimpleSpanProcessor } = coreRequire('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = coreRequire('@opentelemetry/exporter-trace-otlp-http');
const { OllamaInstrumentation } = require('../dist/cjs');
const { SemanticConventions: S } = require('@traceai/fi-semantic-conventions');

let sdk, server, provider, instrumentation, patched, client;
let started, ended, bodies, transports;
const sentinel = 'synthetic-private-content';
const realFetch = globalThis.fetch;
globalThis.fetch = () => { throw new Error('Unexpected network request'); };

before(async () => {
  sdk = await import(pathToFileURL(require.resolve(process.env.OLLAMA_SDK_PATH || 'ollama')));
  server = createServer(async (req, res) => {
    assert.equal(req.url, '/v1/traces');
    let body = '';
    for await (const data of req) body += data;
    bodies.push(JSON.parse(body));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
});
after(async () => {
  globalThis.fetch = realFetch;
  await new Promise(resolve => server.close(resolve));
});

function setup(traceConfig = {}, withDefault = false) {
  started = []; ended = []; bodies = []; transports = [];
  const counter = {
    onStart(span) {
      const record = { span, ends: 0 };
      const end = span.end.bind(span);
      span.end = (...args) => { record.ends++; return end(...args); };
      started.push(record);
    },
    onEnd(span) { ended.push(span); },
    forceFlush: async () => {}, shutdown: async () => {},
  };
  provider = new BasicTracerProvider({ spanProcessors: [counter, new SimpleSpanProcessor(
    new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces`, headers: {} }),
  )] });
  instrumentation = new OllamaInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider);
  // A subclass isolates prototype mutation between tests; the actual SDK methods run unchanged.
  class Ollama extends sdk.Ollama {}
  client = new Ollama({ host: 'http://offline.invalid', fetch: fakeFetch });
  patched = { Ollama, ...(withDefault ? { default: client } : {}) };
  instrumentation.manuallyInstrument(patched);
}
beforeEach(() => setup());
afterEach(async () => {
  for (const transport of transports) transport.close();
  // Manually patched modules are not tracked by the automatic loader; undo the test patch.
  instrumentation.unpatch(patched);
  instrumentation.disable();
  await provider.shutdown();
});

let mode;
function fakeFetch(url, options) {
  assert.match(String(url), /^http:\/\/offline\.invalid(?::80)?\/api\/(chat|generate)$/);
  const method = String(url).endsWith('/chat') ? 'chat' : 'generate';
  const message = { role: 'assistant', content: 'hello ', thinking: 'consider ',
    tool_calls: [{ function: { name: 'lookup', arguments: { city: sentinel } } }] };
  const chunks = [
    { model: 'offline-model', created_at: '2026-01-01T00:00:00Z', message, response: 'hello ', thinking: 'consider ', done: false },
    { model: 'offline-model', message: { role: 'assistant', content: 'world', thinking: 'carefully',
      tool_calls: [{ function: { name: 'save', arguments: { value: sentinel } } }] }, response: 'world', thinking: 'carefully', done: false },
    { model: 'offline-model', message: { role: 'assistant', content: '' }, response: '', done: true, prompt_eval_count: 3, eval_count: 2 },
  ];
  const request = JSON.parse(options.body);
  if (!request.stream) {
    const result = { ...chunks[0], ...chunks[2], message, response: 'hello ', thinking: 'consider ' };
    return Promise.resolve(new Response(JSON.stringify(result), { status: 200 }));
  }
  const signal = options.signal;
  let closed = false;
  let controller;
  function close() { if (!closed) { closed = true; controller.close(); } }
  const body = new ReadableStream({ start(c) {
    controller = c;
    const send = chunk => c.enqueue(new TextEncoder().encode(JSON.stringify(chunk) + '\n'));
    send(chunks[0]);
    if (mode === 'error') { send({ error: sentinel }); close(); }
    else if (mode !== 'pending') { chunks.slice(1).forEach(send); close(); }
  } });
  signal?.addEventListener('abort', () => {
    if (!closed) { closed = true; controller.error(Object.assign(new Error('request aborted'), { name: 'AbortError' })); }
  }, { once: true });
  transports.push({ signal, close, method });
  return Promise.resolve(new Response(body, { status: 200, headers: { 'content-type': 'application/x-ndjson' } }));
}
async function stream(method, selectedMode = 'full') {
  mode = selectedMode;
  return client[method]({ model: 'offline-model', stream: true,
    messages: [{ role: 'user', content: 'hello' }], prompt: 'hello' });
}
async function wireSpan(status) {
  await provider.forceFlush();
  assert.equal(started.length, 1, 'one SDK call must start one span');
  assert.equal(started[0].ends, 1, 'span.end is called exactly once');
  assert.equal(ended.length, 1, 'one finished span');
  assert.equal(ended[0].status.code, status);
  const spans = bodies.flatMap(body => body.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.length, 1, 'one span on the OTLP wire');
  assert.equal(spans[0].status.code || 0, status);
  return Object.fromEntries(spans[0].attributes.map(a => [a.key, Object.values(a.value)[0]]));
}

for (const method of ['chat', 'generate']) {
  test(`${method}: full consumption preserves chunks and token usage on the wire`, async () => {
    const result = await stream(method);
    const chunks = [];
    for await (const chunk of result) chunks.push(chunk);
    assert.equal(chunks.length, 3);
    assert.equal(chunks[0].message.tool_calls[0].function.arguments.city, sentinel);
    const attrs = await wireSpan(1);
    assert.equal(attrs[S.OUTPUT_VALUE], 'hello world');
    assert.equal(Number(attrs[S.LLM_TOKEN_COUNT_TOTAL]), 5);
    assert.equal(JSON.parse(attrs[S.RAW_OUTPUT]).length, 3);
  });
  test(`${method}: normalizes streamed thinking and tools`, async () => {
    for await (const chunk of await stream(method)) { /* consume */ }
    const attrs = await wireSpan(1);
    const [message] = JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES]);
    assert.equal(message.content, 'hello world');
    assert.equal(message.thinking, 'consider carefully');
    if (method === 'chat') {
      assert.deepEqual(message.tool_calls.map(t => t.function.name), ['lookup', 'save']);
      assert.deepEqual(JSON.parse(message.tool_calls[0].function.arguments), { city: sentinel });
    }
  });
  test(`${method}: early break closes the span with partial output`, async () => {
    for await (const chunk of await stream(method)) { assert.equal(chunk.done, false); break; }
    const attrs = await wireSpan(0);
    assert.equal(attrs[S.OUTPUT_VALUE], 'hello ');
    assert.equal(attrs[S.LLM_TOKEN_COUNT_TOTAL], undefined);
    if (transports[0].signal) assert.equal(transports[0].signal.aborted, true);
  });
  for (const consumed of [false, true]) {
    test(`${method}: return ${consumed ? 'after one chunk' : 'before first next'}`, async () => {
      const result = await stream(method);
      const iterator = result[Symbol.asyncIterator]();
      if (consumed) await iterator.next();
      assert.deepEqual(await iterator.return('closed'), { done: true, value: 'closed' });
      await iterator.return();
      await wireSpan(0);
      if (transports[0].signal) assert.equal(transports[0].signal.aborted, true);
    });
    test(`${method}: throw ${consumed ? 'after one chunk' : 'before first next'}`, async () => {
      const result = await stream(method);
      const iterator = result[Symbol.asyncIterator]();
      if (consumed) await iterator.next();
      const error = new Error('consumer stop');
      await assert.rejects(iterator.throw(error), e => e === error);
      await iterator.return();
      await wireSpan(2);
    });
  }
  test(`${method}: consumer body throw finalizes through return`, async () => {
    const error = new Error('consumer body');
    await assert.rejects(async () => {
      for await (const chunk of await stream(method)) throw error;
    }, e => e === error);
    await wireSpan(0);
  });
  test(`${method}: upstream error is preserved and ends once`, async () => {
    await assert.rejects(async () => {
      for await (const chunk of await stream(method, 'error')) { /* consume */ }
    }, { message: sentinel });
    const attrs = await wireSpan(2);
    assert.equal(attrs[S.OUTPUT_VALUE], 'hello ');
  });
  test(`${method}: direct next/return remain available to existing callers`, async () => {
    const result = await stream(method);
    assert.equal((await result.next()).value.done, false);
    await result.return();
    await wireSpan(0);
  });
  test(`${method}: abort exists exactly when supported by the SDK`, async () => {
    const result = await stream(method);
    const nativeAbort = Boolean(transports[0].signal);
    assert.equal(typeof result.abort, nativeAbort ? 'function' : 'undefined');
    if (nativeAbort) { result.abort(); result.abort(); await wireSpan(0); }
    else { await result.return(); await wireSpan(0); }
  });
  test(`${method}: abort interrupts a pending read and ends once`, async t => {
    const result = await stream(method, 'pending');
    if (!transports[0].signal) { await result.return(); return t.skip('SDK 0.4 has no abort support'); }
    const iterator = result[Symbol.asyncIterator]();
    await iterator.next();
    const pending = iterator.next();
    const rejected = assert.rejects(pending, { name: 'AbortError' });
    result.abort();
    await rejected;
    await iterator.return();
    await wireSpan(0);
  });
  test(`${method}: client.abort finalizes an unconsumed native stream`, async t => {
    const result = await stream(method, 'pending');
    if (typeof client.abort !== 'function') { await result.return(); return t.skip('SDK 0.4 has no abort support'); }
    client.abort();
    assert.equal(transports[0].signal.aborted, true);
    await wireSpan(0);
  });
  test(`${method}: preserves the native stream prototype and binds extra methods`, async () => {
    const original = client.processStreamableRequest;
    let native;
    client.processStreamableRequest = async function (...args) {
      native = await original.apply(this, args);
      native.marker = 'native-stream';
      native.readMarker = function () { assert.equal(this, native); return this.marker; };
      return native;
    };
    const result = await stream(method);
    assert.equal(Object.getPrototypeOf(result), Object.getPrototypeOf(native));
    assert.equal(result.readMarker(), 'native-stream');
    for await (const chunk of result) { /* consume */ }
    await wireSpan(1);
  });
  test(`${method}: return interrupts a pending read`, async t => {
    const result = await stream(method, 'pending');
    if (!transports[0].signal) { await result.return(); return t.skip('SDK 0.4 has no abort support'); }
    const iterator = result[Symbol.asyncIterator]();
    await iterator.next();
    const pending = assert.rejects(iterator.next(), { name: 'AbortError' });
    await iterator.return();
    await pending;
    await wireSpan(0);
  });
  test(`${method}: hideOutputs excludes partial tools/thinking/errors from OTLP`, async () => {
    instrumentation.unpatch(patched);
    await provider.shutdown();
    setup({ hideOutputs: true });
    await assert.rejects(async () => {
      for await (const chunk of await stream(method, 'error')) { /* consume */ }
    }, { message: sentinel });
    const attrs = await wireSpan(2);
    assert.equal(attrs[S.LLM_OUTPUT_MESSAGES], undefined);
    assert.equal(JSON.stringify(bodies).includes(sentinel), false);
    assert.equal(JSON.stringify(bodies).includes('consider '), false);
  });
  test(`${method}: patching class and its default instance emits only one span`, async () => {
    instrumentation.unpatch(patched);
    await provider.shutdown();
    setup({}, true);
    for await (const chunk of await stream(method)) { /* consume */ }
    await wireSpan(1);
  });
  test(`${method}: nonstreaming calls still work`, async () => {
    const result = await client[method]({ model: 'offline-model', stream: false,
      messages: [{ role: 'user', content: 'hello' }], prompt: 'hello' });
    assert.equal(result.done, true);
    await wireSpan(1);
  });
}
