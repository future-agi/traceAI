// Run against installed tarballs in a clean consumer, never workspace links:
//   FIREWORKS_CONSUMER_DIR=/path/to/consumer node --test test/sharedRoute.cjs
// FIREWORKS_TEST_MANUAL=true instruments an explicitly loaded SDK module (as ESM
// applications do); otherwise CommonJS require hooks patch it automatically.
// FIREWORKS_TEST_BOTH=true also enables the shared OpenAIInstrumentation.
const { test, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const { createServer } = require('node:http');

const req = createRequire(resolve(process.env.FIREWORKS_CONSUMER_DIR, 'package.json'));
const { BasicTracerProvider, SimpleSpanProcessor } = req('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = req('@opentelemetry/exporter-trace-otlp-http');
const { trace } = req('@opentelemetry/api');
const { FireworksInstrumentation, isPatched } = req('@traceai/fireworks');
const { OpenAIInstrumentation } = req('@traceai/openai');

const manual = process.env.FIREWORKS_TEST_MANUAL === 'true';
const both = process.env.FIREWORKS_TEST_BOTH === 'true';
const SECRET = 'SYNTHETIC-PRIVATE-CONTENT';
const FIREWORKS = 'https://api.fireworks.ai/inference/v1';

const wire = [];
const server = createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const decoded = JSON.parse(Buffer.concat(chunks));
  wire.push(...decoded.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)));
  response.writeHead(200, { 'content-type': 'application/json' });
  response.end('{}');
});
const ready = new Promise((done) => server.listen(0, '127.0.0.1', done));

let provider;
let OpenAI;
const instrumentations = [];
const warnings = [];
process.on('warning', (warning) => warnings.push(warning));

function body(operation) {
  if (operation === 'embeddings') {
    return { object: 'list', model: 'returned-model', data: [{ index: 0, object: 'embedding', embedding: [0.1] }], usage: { prompt_tokens: 2, total_tokens: 2 } };
  }
  return {
    id: 'synthetic-id', created: 1, model: 'returned-model',
    object: operation === 'chat' ? 'chat.completion' : 'text_completion',
    choices: [{ index: 0, message: { role: 'assistant', content: SECRET }, text: SECRET, finish_reason: 'stop' }],
    usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 },
  };
}

function chunk(operation) {
  return {
    id: 'synthetic-id', created: 1, model: 'returned-model',
    object: operation === 'chat' ? 'chat.completion.chunk' : 'text_completion',
    choices: [{ index: 0, delta: { role: 'assistant', content: SECRET }, text: SECRET, finish_reason: 'stop' }],
  };
}

// The configured host stays real so attribution is exercised; the transport is in-process.
function client(baseURL, outcome, operation) {
  return new OpenAI({
    apiKey: 'synthetic-credential',
    baseURL,
    maxRetries: 0,
    fetch: async () => {
      if (outcome === 'error') {
        return new Response(JSON.stringify({ error: { message: 'SYNTHETIC-PRIVATE-ERROR', type: 'invalid_request_error' } }), { status: 400, headers: { 'content-type': 'application/json' } });
      }
      if (outcome === 'stream') {
        return new Response(`data: ${JSON.stringify(chunk(operation))}\n\ndata: [DONE]\n\n`, { headers: { 'content-type': 'text/event-stream' } });
      }
      return new Response(JSON.stringify(body(operation)), { headers: { 'content-type': 'application/json' } });
    },
  });
}

async function call(operation, outcome = 'success', baseURL = FIREWORKS) {
  const c = client(baseURL, outcome, operation);
  const stream = outcome === 'stream' ? { stream: true } : {};
  const request = operation === 'chat'
    ? c.chat.completions.create({ model: 'accounts/fireworks/models/synthetic', messages: [{ role: 'user', content: SECRET }], ...stream })
    : operation === 'completions'
      ? c.completions.create({ model: 'accounts/fireworks/models/synthetic', prompt: SECRET, ...stream })
      : c.embeddings.create({ model: 'nomic-ai/synthetic', input: 'synthetic embedding', encoding_format: 'float' });
  if (outcome === 'error') return assert.rejects(request);
  const result = await request;
  if (outcome === 'stream') for await (const _ of result) { /* consume */ }
}

async function exported() {
  await provider.forceFlush();
  const spans = wire.splice(0);
  const serialized = JSON.stringify(spans);
  assert.doesNotMatch(serialized, /SYNTHETIC-PRIVATE-CONTENT|SYNTHETIC-PRIVATE-ERROR|synthetic-credential/);
  return spans.map((span) => span.attributes.find((a) => a.key === 'gen_ai.provider.name')?.value.stringValue);
}

test('set up instrumentation before the real SDK is used', async () => {
  await ready;
  provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` }))],
  });
  // The documented setup registers the provider before constructing instrumentation.
  trace.setGlobalTracerProvider(provider);
  // Manual mode loads the SDK first, so no require hook can patch it.
  const preloaded = manual ? req('openai') : undefined;
  const options = { traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true } };
  instrumentations.push(new FireworksInstrumentation(options));
  if (both) instrumentations.push(new OpenAIInstrumentation(options));
  for (const instrumentation of instrumentations) instrumentation.setTracerProvider(provider);
  const sdk = preloaded ?? req('openai');
  if (manual) for (const instrumentation of instrumentations) instrumentation.manuallyInstrument(sdk);
  OpenAI = sdk.OpenAI;
  assert.equal(isPatched(), true);
  await new Promise(setImmediate);
  assert.equal(warnings.filter((w) => w.name === 'DeprecationWarning' && w.code === 'TRACEAI_FIREWORKS_DEPRECATED').length, 1);
});

beforeEach(() => { wire.length = 0; });

for (const [operation, outcome] of [
  ['chat', 'success'], ['chat', 'stream'], ['chat', 'error'],
  ['completions', 'success'], ['completions', 'stream'], ['completions', 'error'],
  ['embeddings', 'success'], ['embeddings', 'error'],
]) {
  test(`${operation} ${outcome} exports one Fireworks span`, async () => {
    await call(operation, outcome);
    assert.deepEqual(await exported(), ['fireworks']);
  });
}

test('a direct OpenAI client in the same process keeps its own provider', async () => {
  await call('chat', 'success', 'https://api.openai.com/v1');
  assert.deepEqual(await exported(), ['openai']);
});

test('disabling the instrumentor that did not patch keeps tracing', { skip: !both }, async () => {
  instrumentations[1].disable();
  await call('chat', 'stream');
  assert.deepEqual(await exported(), ['fireworks']);
});

test('disabling every instrumentor stops tracing', async () => {
  for (const instrumentation of instrumentations) instrumentation.disable();
  assert.equal(isPatched(), false);
  await call('chat');
  assert.deepEqual(await exported(), []);
});

after(async () => {
  await provider?.shutdown();
  await new Promise((done) => server.close(done));
});
