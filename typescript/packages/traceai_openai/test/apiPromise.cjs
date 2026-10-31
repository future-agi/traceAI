// Real SDK, synthetic transport. Run built package with Node's test runner.
const { test, before, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OpenAIInstrumentation } = require('../dist/src');
const sdk = require(process.env.OPENAI_SDK_PATH || 'openai');
const OpenAI = sdk.OpenAI || sdk;
const { createServer } = require('node:http');
const { once } = require('node:events');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const exporter = new InMemorySpanExporter();
const wire = [];
const server = createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  wire.push(JSON.parse(Buffer.concat(chunks).toString()));
  res.writeHead(200, { 'content-type': 'application/json' }).end('{}');
});
let provider, instrumentation;
before(async () => {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  provider = new BasicTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` })),
  ] });
  instrumentation = new OpenAIInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
});
after(async () => {
  instrumentation.disable();
  await provider.shutdown();
  await new Promise(resolve => server.close(resolve));
});
afterEach(async () => { await provider.forceFlush(); exporter.reset(); wire.length = 0; });

const fixtures = {
  chat: { id: 'chat-offline', object: 'chat.completion', model: 'test-model', choices: [{ index: 0, message: { role: 'assistant', content: 'synthetic answer' }, finish_reason: 'stop' }], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } },
  completions: { id: 'cmpl-offline', object: 'text_completion', model: 'test-model', choices: [{ index: 0, text: 'synthetic answer', finish_reason: 'stop' }], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } },
  embeddings: { object: 'list', data: [{ object: 'embedding', index: 0, embedding: [0.1, 0.2] }], model: 'test-model', usage: { prompt_tokens: 2, total_tokens: 2 } },
  responses: { id: 'resp-offline', object: 'response', status: 'completed', model: 'test-model', output_text: 'synthetic answer', output: [{ id: 'msg-offline', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: 'synthetic answer', annotations: [] }] }], usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 } },
};
function request(endpoint, { status = 200, body = JSON.stringify(fixtures[endpoint]), stream = false, helper = false, fetchError } = {}) {
  let response, requests = 0, parses = 0;
  const client = new OpenAI({ apiKey: 'offline-not-a-real-key', maxRetries: 0, fetch: async () => {
    requests++;
    if (fetchError) throw fetchError;
    response = new Response(body, { status, headers: { 'content-type': stream ? 'text/event-stream' : 'application/json', 'x-request-id': 'req-offline' } });
    for (const method of ['json', 'text']) {
      const read = response[method].bind(response);
      response[method] = () => { parses++; return read(); };
    }
    return response;
  } });
  const args = { model: 'test-model', ...(stream ? { stream: true } : {}) };
  const chat = client.chat.completions.parse ? client.chat.completions : client.beta?.chat?.completions;
  const promise = helper ? chat.parse({ ...args, messages: [], response_format: { type: 'json_schema', json_schema: { name: 'answer', strict: true, schema: { type: 'object', properties: { answer: { type: 'string' } }, required: ['answer'], additionalProperties: false } } } })
    : endpoint === 'chat' ? client.chat.completions.create({ ...args, messages: [] })
    : endpoint === 'completions' ? client.completions.create({ ...args, prompt: 'synthetic input' })
    : endpoint === 'embeddings' ? client.embeddings.create({ ...args, input: 'synthetic input', encoding_format: 'float' })
    : client.responses.create({ ...args, input: 'synthetic input' });
  return { promise, response: () => response, requests: () => requests, parses: () => parses };
}
async function span(status = 1) {
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1, 'exactly one finished span');
  assert.equal(spans[0].status.code, status);
  const exported = wire.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
  assert.equal(exported.length, 1, 'exactly one span on the OTLP/HTTP wire');
  assert.equal(exported[0].spanId, spans[0].spanContext().spanId);
  assert.equal(exported[0].status.code, status);
  assert.equal(exported[0].name, spans[0].name);
  assert.ok(!JSON.stringify(wire).includes('offline-not-a-real-key'));
  for (const key of ['raw.output', 'output.value', 'http.response.status_code']) {
    const attribute = exported[0].attributes.find(attribute => attribute.key === key);
    if (spans[0].attributes[key] === undefined) assert.equal(attribute, undefined);
    else assert.equal(String(Object.values(attribute.value)[0]), String(spans[0].attributes[key]));
  }
  return spans[0];
}
const endpoints = ['chat', 'completions', 'embeddings', ...(OpenAI.Responses ? ['responses'] : [])];
for (const endpoint of endpoints) {
  test(`${endpoint}: preserves APIPromise and withResponse metadata`, async () => {
    const r = request(endpoint);
    assert.equal(typeof r.promise.asResponse, 'function');
    assert.equal(typeof r.promise.withResponse, 'function');
    assert.equal(typeof r.promise._thenUnwrap, 'function');
    assert.equal(r.promise.constructor.name, 'APIPromise');
    assert.ok(r.promise instanceof Promise);
    const result = await r.promise.withResponse();
    assert.equal(result.response, r.response());
    assert.deepEqual(result.data, fixtures[endpoint]);
    if ('request_id' in result) assert.equal(result.request_id, 'req-offline');
    assert.equal(await r.promise, result.data, 'repeat await uses native parse cache');
    assert.equal((await r.promise.withResponse()).data, result.data);
    assert.equal(r.requests(), 1);
    assert.equal(r.parses(), 1);
    await span();
  });
  test(`${endpoint}: asResponse never parses or consumes the body`, async () => {
    const r = request(endpoint);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(r.parses(), 0, 'no eager parsing');
    const raw = await r.promise.asResponse();
    assert.equal(raw, r.response());
    assert.equal(raw.bodyUsed, false);
    assert.equal(r.parses(), 0);
    const exported = await span();
    assert.equal(exported.attributes['raw.output'], undefined);
    assert.equal(await raw.text(), JSON.stringify(fixtures[endpoint]));
  });
  test(`${endpoint}: native unwrapping retains helpers`, async () => {
    const r = request(endpoint);
    const mapped = r.promise._thenUnwrap(data => ({ original: data }));
    const result = await mapped.withResponse();
    assert.deepEqual(result.data.original, fixtures[endpoint]);
    assert.equal(result.response, r.response());
    await span();
  });
  for (const method of ['then', 'catch', 'finally', 'withResponse', 'asResponse']) {
    test(`${endpoint}: ${method} preserves HTTP rejection and records once`, async () => {
      const r = request(endpoint, { status: 400, body: JSON.stringify({ error: { message: 'synthetic HTTP failure' } }) });
      let observed;
      try { await r.promise[method](); } catch (error) { observed = error; }
      assert.equal(observed.status, 400);
      await assert.rejects(r.promise, error => error === observed);
      const exported = await span(2);
      assert.equal(exported.events.filter(event => event.name === 'exception').length, 1);
    });
  }
  for (const method of ['then', 'catch', 'finally', 'withResponse']) {
    test(`${endpoint}: ${method} preserves parse rejection`, async () => {
      const r = request(endpoint, { body: '{invalid json' });
      await assert.rejects(r.promise[method](), SyntaxError);
      await span(2);
    });
  }
}
for (const endpoint of ['chat', 'completions', ...(OpenAI.Responses ? ['responses'] : [])]) {
  test(`${endpoint}: raw streaming response remains unread`, async () => {
    const body = 'data: {"synthetic":"chunk"}\n\ndata: [DONE]\n\n';
    const r = request(endpoint, { stream: true, body });
    const response = await r.promise.asResponse();
    assert.equal(response.bodyUsed, false);
    assert.equal(await response.text(), body);
    await span();
  });
}

const helperClient = new OpenAI({ apiKey: 'offline-not-a-real-key' });
if (helperClient.chat.completions.parse || helperClient.beta?.chat?.completions?.parse) {
  test('SDK structured-output helper retains withResponse and parsed content', async () => {
    const fixture = structuredClone(fixtures.chat);
    fixture.choices[0].message.content = '{"answer":"synthetic"}';
    const r = request('chat', { helper: true, body: JSON.stringify(fixture) });
    const { data, response } = await r.promise.withResponse();
    assert.equal(response, r.response());
    assert.deepEqual(data.choices[0].message.parsed, { answer: 'synthetic' });
    await span();
  });
}
for (const method of ['then', 'catch']) {
  test(`caller can recover HTTP errors through ${method}`, async () => {
    const r = request('chat', { status: 400, body: JSON.stringify({ error: { message: 'synthetic failure' } }) });
    const recovery = error => ({ recovered: error.status });
    const result = method === 'then' ? await r.promise.then(undefined, recovery) : await r.promise.catch(recovery);
    assert.deepEqual(result, { recovered: 400 });
    await span(2);
  });
}
test('raw then parsed access does not duplicate spans', async () => {
  const r = request('chat');
  const raw = await r.promise.asResponse();
  assert.equal(raw.bodyUsed, false);
  const { data, response } = await r.promise.withResponse();
  assert.equal(response, raw);
  assert.deepEqual(data, fixtures.chat);
  assert.equal((await span()).attributes['raw.output'], undefined);
});
test('transport failure without caller parsing finishes the span', async () => {
  const r = request('chat', { fetchError: new Error('synthetic network failure') });
  await new Promise(resolve => setImmediate(resolve));
  await span(2);
  await assert.rejects(r.promise.withResponse(), /Connection error/);
});
