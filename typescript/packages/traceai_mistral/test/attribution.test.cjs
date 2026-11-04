const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { SpanStatusCode } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { MistralInstrumentation } = require('../dist/src');

const request = { model: 'requested-alias', messages: [{ role: 'user', content: 'synthetic-input-content' }] };
const classifierMethods = ['moderate', 'moderateChat', 'classify', 'classifyChat'];
function classifierRequest(method) {
  if (method === 'classifyChat') return { model: request.model, input: { messages: request.messages } };
  return { model: request.model, inputs: method === 'moderateChat' ? request.messages : ['synthetic-input-content'] };
}

async function fixture(t, { sdk = 'mistral-current', config, registration = 'client' } = {}) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new MistralInstrumentation({ traceConfig: config });
  instrumentation.setTracerProvider(provider);
  const calls = [];
  const server = http.createServer(async (req, res) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    calls.push({ body, headers: req.headers, path: req.url });
    if (req.headers['x-test-error']) {
      res.writeHead(400, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ message: 'synthetic-error-content' }));
      return;
    }
    let response = { id: 'synthetic-id', model: 'resolved-model' };
    if (/moderations|classifications/.test(req.url)) {
      response.results = req.url.includes('classifications')
        ? [{ label: { scores: { 'synthetic-result-content': 0.01 } } }]
        : [{ categories: { 'synthetic-result-content': false }, category_scores: { 'synthetic-result-content': 0.01 } }];
    } else {
      response = { ...response, object: 'chat.completion', created: 1,
        choices: [{ index: 0, finish_reason: 'stop', [body.stream ? 'delta' : 'message']: { role: 'assistant', content: 'synthetic-output-content' } }],
        usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } };
    }
    res.writeHead(200, { 'content-type': body.stream ? 'text/event-stream' : 'application/json' });
    res.end(body.stream ? `data: ${JSON.stringify(response)}\n\ndata: [DONE]\n\n` : JSON.stringify(response));
  });
  t.after(async () => {
    instrumentation.disable();
    await provider.shutdown();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const url = `http://127.0.0.1:${server.address().port}`;
  // The 0.x SDK installs a fetch polyfill globally on import. Do not leak it
  // into current-SDK tests (its fetch cannot accept the native Request type).
  const fetchGlobals = Object.fromEntries(['fetch', 'Request', 'Response', 'Headers'].map(key => [key, globalThis[key]]));
  t.after(() => Object.assign(globalThis, fetchGlobals));
  const mod = await import(sdk);
  const legacy = typeof mod.Mistral !== 'function';
  const client = legacy ? new mod.default('synthetic-key', url) : new mod.Mistral({ apiKey: 'synthetic-key', serverURL: url });
  if (registration === 'module') instrumentation.manuallyInstrument(mod);
  else instrumentation.instrumentClient(client);
  return { client, calls, instrumentation, spans: () => exporter.getFinishedSpans(),
    complete: body => legacy ? client.chat(body) : client.chat.complete(body),
    stream: body => legacy ? client.chatStream(body) : client.chat.stream(body) };
}

for (const sdk of ['mistral-current', 'mistral-floor']) {
  for (const stream of [false, true]) {
    for (const privateContent of [false, true]) {
      test(`${sdk}: ${stream ? 'stream' : 'completion'} retains alias and response model (private=${privateContent})`, async t => {
        const f = await fixture(t, { sdk, config: { hideInputs: privateContent, hideOutputs: privateContent } });
        if (stream) {
          let count = 0;
          for await (const _ of await f.stream(request)) count++;
          assert.equal(count, 1);
        } else assert.equal((await f.complete(request)).model, 'resolved-model');
        assert.equal(f.calls[0].body.model, 'requested-alias');
        assert.equal(f.spans().length, 1);
        const attrs = f.spans()[0].attributes;
        assert.equal(attrs['gen_ai.request.model'], 'requested-alias');
        assert.equal(attrs['gen_ai.response.model'], 'resolved-model');
        if (privateContent) assert.doesNotMatch(JSON.stringify(attrs), /synthetic-(input|output)-content/);
      });
    }
  }
}

test('agent requests without a model do not invent a request model from the response', async t => {
  const f = await fixture(t);
  await f.client.agents.complete({ agentId: 'synthetic-agent', messages: request.messages });
  assert.equal(f.spans().length, 1);
  assert.equal(f.spans()[0].attributes['gen_ai.request.model'], undefined);
  assert.equal(f.spans()[0].attributes['gen_ai.response.model'], 'resolved-model');
});

for (const method of classifierMethods) {
  for (const registration of ['client', 'module']) {
    test(`classifiers.${method}: ${registration} registration records native results and preserves options`, async t => {
      const f = await fixture(t, { registration });
      const body = classifierRequest(method);
      const options = { fetchOptions: { headers: { 'x-test-option': 'preserved' } } };
      const result = await f.client.classifiers[method](body, options);
      assert.equal(result.results.length, 1);
      assert.equal(f.calls[0].headers['x-test-option'], 'preserved');
      assert.equal(f.calls[0].body.model, 'requested-alias');
      assert.equal(f.spans().length, 1);
      const span = f.spans()[0];
      assert.equal(span.name, `Mistral classifiers.${method}`);
      assert.equal(span.status.code, SpanStatusCode.OK);
      assert.equal(span.attributes['gen_ai.provider.name'], 'mistralai');
      assert.equal(span.attributes['gen_ai.request.model'], 'requested-alias');
      assert.equal(span.attributes['gen_ai.response.model'], 'resolved-model');
      assert.equal(span.attributes['gen_ai.operation.name'], method.startsWith('moderate') ? 'moderation' : 'classification');
      assert.match(span.attributes['input.value'], /synthetic-input-content/);
      assert.deepEqual(JSON.parse(span.attributes['output.value']), result);
      assert.doesNotMatch(JSON.stringify(span.attributes), /synthetic-key/);
      assert.deepEqual(options, { fetchOptions: { headers: { 'x-test-option': 'preserved' } } });
    });
  }
  for (const config of [{ hideInputs: true, hideOutputs: true }, { hideInputText: true, hideOutputText: true }]) {
    for (const error of [false, true]) {
      test(`classifiers.${method}: privacy ${JSON.stringify(config)} error=${error}`, async t => {
        const f = await fixture(t, { config });
        const body = classifierRequest(method);
        const call = () => f.client.classifiers[method](body, { fetchOptions: { headers: error ? { 'x-test-error': 'yes' } : {} } });
        if (error) await assert.rejects(call, /synthetic-error-content/);
        else assert.equal((await call()).results.length, 1);
        assert.equal(f.spans().length, 1);
        const span = f.spans()[0];
        assert.equal(span.status.code, error ? SpanStatusCode.ERROR : SpanStatusCode.OK);
        assert.equal(span.attributes['gen_ai.provider.name'], 'mistralai');
        assert.equal(span.attributes['gen_ai.request.model'], 'requested-alias');
        assert.doesNotMatch(JSON.stringify({ attributes: span.attributes, status: span.status, events: span.events }), /synthetic-(input|result|error)-content|synthetic-key/);
      });
    }
  }
}
