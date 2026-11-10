'use strict';
// Isolate Node's rejection policy from the test runner, using synthetic HTTP only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  });
  module._compile(outputText, filename);
};
let externalAttempts = 0;
const block = () => { externalAttempts++; throw new Error('External transport is disabled'); };
require('node:http').request = block;
require('node:https').request = block;
global.fetch = block;
const sdk = require(process.env.OPENAI_TEST_MODULE || 'openai');
const { OpenAIInstrumentation } = require('../src/instrumentation.ts');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const [endpoint, mode, traced] = process.argv.slice(2);
const describe = error => ({ name: error.name, message: error.message, status: error.status, code: error.code });
const events = [];
if (mode !== 'default-exit') process.on('unhandledRejection', error => events.push(describe(error)));
const pause = () => new Promise(resolve => setTimeout(resolve, 80));
const errorBody = { error: { message: 'synthetic request failure', type: 'invalid_request_error', code: 'offline_failure' } };
const body = endpoint === 'chat'
  ? { id: 'chat-synthetic', object: 'chat.completion', model: 'synthetic-model', choices: [{ index: 0, message: { role: 'assistant', content: 'hello' }, finish_reason: 'stop' }] }
  : { object: 'list', model: 'synthetic-model', data: [{ object: 'embedding', index: 0, embedding: [0.5] }], usage: { prompt_tokens: 1, total_tokens: 1 } };

async function main() {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  if (traced === 'instrumented') instrumentation.manuallyInstrument(sdk);
  const success = ['with-response', 'raw-response', 'error-body', 'stream'].includes(mode);
  let httpCalls = 0, parses = 0, response;
  const client = new sdk.OpenAI({ apiKey: 'synthetic-key', baseURL: 'https://fixture.invalid/v1', maxRetries: 0,
    fetch: async () => {
      httpCalls++;
      const streamBody = 'data: ' + JSON.stringify({ id: 'chat-synthetic', object: 'chat.completion.chunk', model: 'synthetic-model', choices: [{ index: 0, delta: { content: 'hello' }, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n';
      response = new Response(mode === 'stream' ? streamBody : JSON.stringify(success && mode !== 'error-body' ? body : errorBody), {
        status: success ? 200 : 401,
        headers: { 'content-type': mode === 'stream' ? 'text/event-stream' : 'application/json', 'x-request-id': 'synthetic-request' },
      });
      for (const method of ['json', 'text']) {
        const read = response[method].bind(response);
        response[method] = () => { parses++; return read(); };
      }
      return response;
    },
  });
  const promise = endpoint === 'chat'
    ? client.chat.completions.create({ model: 'synthetic-model', messages: [], ...(mode === 'stream' ? { stream: true } : {}) })
    : client.embeddings.create({ model: 'synthetic-model', input: 'hello', encoding_format: 'float' });
  assert.equal(promise.constructor.name, 'APIPromise');
  assert.ok(promise instanceof Promise);
  for (const helper of ['withResponse', 'asResponse', '_thenUnwrap']) assert.equal(typeof promise[helper], 'function');
  let caught = null, sameError = null;
  if (['awaited', 'then', 'catch', 'finally', 'withResponse', 'asResponse', 'unwrap'].includes(mode)) {
    let first;
    try {
      if (mode === 'awaited') await promise;
      else if (mode === 'unwrap') await promise._thenUnwrap(value => value).withResponse();
      else await promise[mode]();
    } catch (error) { first = error; caught = describe(error); }
    assert.equal(caught?.status, 401);
    try { await promise; } catch (error) { sameError = error === first; }
    assert.equal(sameError, true);
  } else if (mode === 'with-response' || mode === 'error-body') {
    const result = await promise.withResponse();
    assert.equal(result.response, response);
    assert.deepEqual(result.data, mode === 'error-body' ? errorBody : body);
    assert.equal(await promise, result.data);
    assert.equal((await promise.withResponse()).data, result.data);
    assert.equal(parses, 1);
  } else if (mode === 'raw-response') {
    await pause();
    assert.equal(parses, 0);
    assert.equal(response.bodyUsed, false);
    const raw = await promise.asResponse();
    assert.equal(raw, response);
    assert.equal(raw.bodyUsed, false);
    assert.equal(parses, 0);
    assert.deepEqual(await raw.json(), body);
  } else if (mode === 'stream') {
    const stream = await promise;
    assert.equal(typeof stream[Symbol.asyncIterator], 'function');
    assert.ok(stream.controller instanceof AbortController);
    assert.equal(exporter.getFinishedSpans().length, 0);
    // Preserve each helper the installed SDK actually exposes.
    const NativeStream = require((process.env.OPENAI_TEST_MODULE || 'openai') + '/streaming').Stream;
    assert.equal(Object.getPrototypeOf(stream), NativeStream.prototype);
    for (const helper of ['tee', 'toReadableStream']) {
      assert.equal(typeof stream[helper], typeof NativeStream.prototype[helper]);
    }
    const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.equal(chunks.length, 1);
    assert.equal(chunks[0].choices[0].delta.content, 'hello');
  }
  await pause();
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans().map(span => ({
    status: span.status.code, exceptions: span.events.filter(event => event.name === 'exception').length,
  }));
  assert.equal(httpCalls, 1);
  assert.equal(externalAttempts, 0);
  if (traced === 'instrumented') {
    assert.equal(spans.length, 1);
    assert.equal(spans[0].status, !success || mode === 'error-body' ? 2 : 1);
    if (!success || mode === 'error-body') assert.equal(spans[0].exceptions, 1);
  } else assert.equal(spans.length, 0);
  console.log(JSON.stringify({ endpoint, mode, traced, unhandled_events: events, caught, sameError, spans, httpCalls, externalAttempts }));
  instrumentation.disable();
  await provider.shutdown();
}
main().catch(error => { console.error(error); process.exitCode = 1; });
