// Run against an installed tarball in a clean consumer, never workspace links.
const { test, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const { createServer } = require('node:http');
const { gunzipSync } = require('node:zlib');
const req = createRequire(resolve(process.env.OPENAI_CONSUMER_DIR, 'package.json'));
const { BasicTracerProvider, SimpleSpanProcessor } = req('@opentelemetry/sdk-trace-base');
const { trace } = req('@opentelemetry/api');
const format = process.env.OTLP_TEST_FORMAT || 'json';
const gzip = process.env.OTLP_TEST_GZIP === 'true';
const { OTLPTraceExporter } = req(`@opentelemetry/exporter-trace-otlp-${format === 'json' ? 'http' : 'proto'}`);
const proto = req('@opentelemetry/otlp-transformer/build/src/generated/root.js').opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const wire = [];
const server = createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  assert.equal(request.headers['content-encoding'], gzip ? 'gzip' : undefined);
  assert.match(request.headers['content-type'], format === 'json' ? /application\/json/ : /application\/x-protobuf/);
  const body = gzip ? gunzipSync(Buffer.concat(chunks)) : Buffer.concat(chunks);
  const decoded = format === 'json' ? JSON.parse(body) : proto.toObject(proto.decode(body), { longs: String });
  wire.push(...decoded.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  response.writeHead(200, { 'content-type': format === 'json' ? 'application/json' : 'application/x-protobuf' });
  response.end(format === 'json' ? '{}' : Buffer.alloc(0));
});
// listen assigns the loopback port synchronously on supported Node versions.
const ready = new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let provider, instrumentation, OpenAI, client;
const bodyMarker = 'SYNTHETIC-PRIVATE-CONTENT';
const errorMarker = 'SYNTHETIC-PRIVATE-ERROR';
const responseBody = { id: 'resp-test', object: 'response', status: 'completed', model: 'returned-model', output: [{ id: 'msg-test', type: 'message', role: 'assistant', content: [{ type: 'output_text', text: bodyMarker, annotations: [] }] }], usage: { input_tokens: 2, output_tokens: 3, total_tokens: 5 } };
const chatBody = { id: 'chat-test', object: 'chat.completion', model: 'returned-model', choices: [{ index: 0, message: { role: 'assistant', content: bodyMarker }, finish_reason: 'stop' }], usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 } };
const fixtures = {
  chat: chatBody,
  completions: { ...chatBody, object: 'text_completion', choices: [{ index: 0, text: bodyMarker, finish_reason: 'stop' }] },
  embeddings: { object: 'list', model: 'returned-model', data: [{ index: 0, embedding: [0.1] }], usage: { prompt_tokens: 2, total_tokens: 2 } },
  responses: responseBody,
};
let status = 200;
let brokenStream = false;
function call(operation = 'responses', stream = false) {
  const args = { model: 'requested-model', ...(stream ? { stream: true } : {}) };
  if (operation === 'chat') return client.chat.completions.create({ ...args, messages: [{ role: 'user', content: bodyMarker }] });
  if (operation === 'completions') return client.completions.create({ ...args, prompt: bodyMarker });
  if (operation === 'embeddings') return client.embeddings.create({ ...args, input: 'synthetic embedding', encoding_format: 'float' });
  return client.responses.create({ ...args, input: bodyMarker });
}
async function spans(count = 1, expectedProvider = 'perplexity') {
  await provider.forceFlush();
  assert.equal(wire.length, count, 'one client request must produce one span');
  for (const span of wire) {
    assert.equal(span.attributes.find(a => a.key === 'gen_ai.provider.name').value.stringValue, expectedProvider);
  }
  assert.doesNotMatch(JSON.stringify(wire), /SYNTHETIC-PRIVATE-CONTENT|SYNTHETIC-PRIVATE-ERROR|synthetic-credential|user-secret|password-secret/);
  return wire[0];
}
test('initialize instrumentation before the real SDK is loaded', async () => {
  await ready;
  provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces`, compression: gzip ? 'gzip' : 'none' }))] });
  trace.setGlobalTracerProvider(provider);
  if (process.env.OPENAI_TEST_MANUAL === 'true') OpenAI = req('openai');
  const { OpenAIInstrumentation } = req('@traceai/openai');
  instrumentation = new OpenAIInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true } });
  instrumentation.setTracerProvider(provider);
  OpenAI = req('openai');
  if (process.env.OPENAI_TEST_MANUAL === 'true') instrumentation.manuallyInstrument(OpenAI);
  client = new OpenAI({ apiKey: 'synthetic-credential', baseURL: 'https://api.perplexity.ai/v1', maxRetries: 0, fetch: async (url, options) => {
    const params = JSON.parse(options.body);
    const operation = String(url).endsWith('/chat/completions') ? 'chat' : String(url).split('/').pop();
    if (status !== 200) return new Response(JSON.stringify({ error: { message: errorMarker } }), { status, headers: { 'content-type': 'application/json' } });
    const fixture = fixtures[operation];
    if (!params.stream) return new Response(JSON.stringify(fixture), { headers: { 'content-type': 'application/json' } });
    const chunk = operation === 'responses' ? { type: 'response.completed', response: responseBody }
      : operation === 'chat' ? { ...chatBody, object: 'chat.completion.chunk', choices: [{ index: 0, delta: { content: bodyMarker }, finish_reason: 'stop' }] } : fixture;
    const failure = JSON.stringify({ error: { message: errorMarker } });
    return new Response(`data: ${JSON.stringify(chunk)}\n\ndata: ${brokenStream ? failure : '[DONE]'}\n\n`, { headers: { 'content-type': 'text/event-stream' } });
  } });
});
afterEach(async () => { if (provider) await provider.forceFlush(); wire.length = 0; status = 200; brokenStream = false; });
after(async () => { instrumentation?.disable(); await provider?.shutdown(); trace.disable(); await new Promise(resolve => server.close(resolve)); });
for (const operation of ['chat', 'completions', 'embeddings', 'responses']) {
  test(`${operation}: automatic hook preserves parsed response helpers and provider`, async t => {
    if (operation === 'responses' && !client.responses) return t.skip('Responses absent from OpenAI 4.0');
    const promise = call(operation);
    const { data, response } = await promise.withResponse();
    assert.equal(data, await promise);
    assert.equal(response.status, 200);
    assert.equal((await spans()).status.code, 1);
  });
  test(`${operation}: HTTP errors retain attribution and privacy`, async t => {
    if (operation === 'responses' && !client.responses) return t.skip('Responses absent from OpenAI 4.0');
    status = 400;
    await assert.rejects(call(operation), e => e.status === 400);
    assert.equal((await spans()).status.code, 2);
  });
  if (operation === 'embeddings') continue;
  for (const outcome of ['complete', 'cancel', 'error']) {
    test(`${operation}: streamed ${outcome} ends exactly once`, async t => {
      if (operation === 'responses' && !client.responses) return t.skip('Responses absent from OpenAI 4.0');
      brokenStream = outcome === 'error';
      const stream = await call(operation, true);
      assert.equal(typeof stream.controller.abort, 'function');
      const consume = async () => { for await (const chunk of stream) { assert.ok(chunk); if (outcome === 'cancel') break; } };
      if (brokenStream) await assert.rejects(consume()); else await consume();
      const span = await spans();
      if (brokenStream) assert.equal(span.status.code, 2);
    });
  }
}
for (const [url, expected] of [['https://API.PERPLEXITY.AI./v1', 'perplexity'], ['https://api.perplexity.ai.evil.test/v1', 'openai'], ['https://ai-gateway.vercel.sh/v1', 'vercel']]) {
  test(`attribution follows normalized host: ${url}`, async () => {
    client.baseURL = url;
    try { await call('chat'); await spans(1, expected); } finally { client.baseURL = 'https://api.perplexity.ai/v1'; }
  });
}
test('disable removes all resource patches; enable and manual reapply never double-wrap', async () => {
  const operation = client.responses ? 'responses' : 'chat';
  instrumentation.disable();
  await call(operation);
  await spans(0);
  instrumentation.enable();
  instrumentation.manuallyInstrument(OpenAI);
  instrumentation.manuallyInstrument(OpenAI);
  await call(operation);
  await spans();
});
